"""Private, bounded small-file uploads and revocable read-only conversation snapshots."""

import base64
import binascii
import hashlib
import io
import re
import secrets
import unicodedata
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader, PdfWriter, apply_configuration
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject
from starlette.datastructures import UploadFile

from api.deps import User
from repositories.supabase import Supabase

router = APIRouter(prefix="/api")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_PDF_DECODED_BYTES = 50 * 1024 * 1024
ATTACHMENT_FIELDS = "id,conversation_id,filename,media_type,size_bytes,page_count,created_at"
MEDIA = {
    "image/png": ("PNG", {".png"}),
    "image/jpeg": ("JPEG", {".jpg", ".jpeg"}),
    "image/webp": ("WEBP", {".webp"}),
    "application/pdf": ("PDF", {".pdf"}),
}
ERRORS = {
    "CONVERSATION_NOT_FOUND": (404, "Conversa não encontrada."),
    "ATTACHMENT_NOT_FOUND": (404, "Arquivo não encontrado."),
    "ATTACHMENT_BOUND": (409, "Um arquivo enviado em mensagem não pode ser removido."),
    "ATTACHMENT_QUOTA_EXCEEDED": (413, "Limite de 50 MB de arquivos por conta atingido."),
    "INVALID_ATTACHMENT": (422, "Arquivo inválido ou fora dos limites permitidos."),
    "SHARE_NOT_FOUND": (404, "Compartilhamento não encontrado."),
}
PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


async def _rpc(db: Supabase, name: str, **parameters: Any) -> dict:
    result = await db.rpc(name, **parameters)
    if result.get("error"):
        status, message = ERRORS.get(result["error"], (503, "Persistência indisponível."))
        raise HTTPException(status, message, headers=PUBLIC_HEADERS)
    return result


def _filename(filename: str | None, media_type: str) -> str:
    if media_type not in MEDIA or not filename:
        raise HTTPException(415, "Envie uma imagem PNG, JPEG, WebP ou um PDF.")
    name = unicodedata.normalize("NFKC", filename)
    if (
        name != name.strip() or len(name.encode("utf-8")) > 240
        or any(unicodedata.category(char).startswith("C") or char in '/\\' for char in name)
        or not any(name.lower().endswith(ext) for ext in MEDIA[media_type][1])
        or name.startswith(".")
    ):
        raise HTTPException(422, "Nome de arquivo inválido.")
    return name


def _pdf(data: bytes) -> tuple[bytes, int]:
    if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
        raise ValueError("Invalid PDF signature")
    # Bound both individual decompression and the complete reachable object graph.
    with apply_configuration(
        maximum_declared_stream_length=MAX_FILE_BYTES,
        zlib_maximum_output_length=20 * 1024 * 1024,
        lzw_maximum_output_length=20 * 1024 * 1024,
        run_length_maximum_output_length=20 * 1024 * 1024,
        array_based_stream_maximum_output_length=20 * 1024 * 1024,
        stream_decoding_work_maximum_length=MAX_PDF_DECODED_BYTES,
        stream_filters_maximum_length=4,
        array_maximum_element_count=10_000,
        dictionary_maximum_element_count=10_000,
        page_tree_maximum_entries=100,
        page_tree_maximum_depth=16,
    ):
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF")
        pages = len(reader.pages)
        if not 1 <= pages <= 20:
            raise ValueError("PDF page limit")
        seen: set[tuple | int] = set()
        decoded = 0
        pixels = 0
        visits = 0

        def visit(obj: Any, depth: int = 0) -> None:
            nonlocal decoded, pixels, visits
            visits += 1
            if visits > 30_000 or depth > 64:
                raise ValueError("PDF object limit")
            key = (obj.idnum, obj.generation) if isinstance(obj, IndirectObject) else id(obj)
            if key in seen:
                return
            seen.add(key)
            if isinstance(obj, IndirectObject):
                visit(obj.get_object(), depth + 1)
                return
            if isinstance(obj, StreamObject):
                decoded += len(obj.get_data())
                if decoded > MAX_PDF_DECODED_BYTES:
                    raise ValueError("PDF decompression limit")
                if obj.get("/Subtype") == "/Image":
                    width, height = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
                    pixels += width * height
                    if width <= 0 or height <= 0 or pixels > 40_000_000:
                        raise ValueError("PDF image limit")
            if isinstance(obj, DictionaryObject):
                for item in obj.values():
                    visit(item, depth + 1)
            elif isinstance(obj, ArrayObject):
                for item in obj:
                    visit(item, depth + 1)

        visit(reader.trailer)
        writer = PdfWriter()
        for page in reader.pages:
            # Copy document pages only, not embedded files, forms or document actions.
            writer.add_page(page, excluded_keys=["/Annots", "/AA", "/Metadata"])
        writer.add_metadata({})
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue(), pages


def normalize_file(data: bytes, filename: str | None, media_type: str | None) -> tuple[str, str, bytes, int | None]:
    media_type = (media_type or "").lower()
    name = _filename(filename, media_type)
    if not data or len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Cada arquivo deve ter no máximo 5 MB.")
    try:
        if media_type == "application/pdf":
            normalized, pages = _pdf(data)
        else:
            with Image.open(io.BytesIO(data), formats=[MEDIA[media_type][0]]) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Image dimensions or animation limit")
                image.verify()
            with Image.open(io.BytesIO(data), formats=[MEDIA[media_type][0]]) as image:
                image.load()
                image = ImageOps.exif_transpose(image)
                image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                alpha = "A" in image.getbands() or "transparency" in image.info
                image = image.convert("RGBA" if media_type != "image/jpeg" and alpha else "RGB")
                image.info.clear()
                output = io.BytesIO()
                image.save(output, format=MEDIA[media_type][0], exif=b"", icc_profile=None)
                normalized, pages = output.getvalue(), None
    except (HTTPException, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        # Parsers may contain private metadata in diagnostics; never expose it.
        raise HTTPException(422, "Arquivo inválido, criptografado ou acima dos limites de processamento.") from None
    if len(normalized) > MAX_FILE_BYTES:
        raise HTTPException(413, "Arquivo normalizado excede 5 MB.")
    return name, media_type, normalized, pages


def _binary(result: dict) -> Response:
    attachment = result["attachment"]
    try:
        data = base64.b64decode(result["data_base64"], validate=True)
    except (ValueError, binascii.Error, KeyError):
        raise HTTPException(503, "Arquivo indisponível.") from None
    disposition = "attachment" if attachment["media_type"] == "application/pdf" else "inline"
    return Response(data, media_type=attachment["media_type"], headers={
        **PUBLIC_HEADERS,
        "Content-Disposition": f"{disposition}; filename=\"download\"; filename*=UTF-8''{quote(attachment['filename'], safe='')}",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    })


@router.post("/conversations/{conversation_id}/attachments", status_code=201)
async def upload_attachment(conversation_id: UUID, request: Request, user_id: User) -> dict:
    db = request.app.state.db
    await db.owned_conversation(user_id, str(conversation_id))
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "multipart/form-data":
        raise HTTPException(415, "Envie multipart/form-data com o campo file.")
    # Bound the entire request before the multipart parser can spool an unlimited file to disk.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_FILE_BYTES + 65536:
            raise HTTPException(413, "Cada arquivo deve ter no máximo 5 MB.")
        body.extend(chunk)

    async def receive() -> dict:
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    bounded = Request(request.scope, receive)
    async with bounded.form(max_files=1, max_fields=0, max_part_size=65536) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(422, "Envie um arquivo no campo file.")
        data = await file.read(MAX_FILE_BYTES + 1)
        name, media_type, normalized, pages = await run_in_threadpool(normalize_file, data, file.filename, file.content_type)
    return await _rpc(db, "store_attachment", p_user_id=user_id, p_conversation_id=str(conversation_id),
                      p_filename=name, p_media_type=media_type, p_data_base64=base64.b64encode(normalized).decode("ascii"), p_page_count=pages)


@router.get("/conversations/{conversation_id}/attachments")
async def list_attachments(conversation_id: UUID, request: Request, user_id: User) -> dict:
    db = request.app.state.db
    await db.owned_conversation(user_id, str(conversation_id))
    return {"attachments": await db.rows("attachments", select=ATTACHMENT_FIELDS, user_id=f"eq.{user_id}",
                                         conversation_id=f"eq.{conversation_id}", order="created_at.asc,id.asc")}


@router.get("/attachments/{attachment_id}")
async def download_attachment(attachment_id: UUID, request: Request, user_id: User) -> Response:
    return _binary(await _rpc(request.app.state.db, "get_attachment", p_user_id=user_id, p_attachment_id=str(attachment_id)))


@router.delete("/attachments/{attachment_id}")
async def remove_attachment(attachment_id: UUID, request: Request, user_id: User) -> dict:
    return await _rpc(request.app.state.db, "remove_attachment", p_user_id=user_id, p_attachment_id=str(attachment_id))


async def model_parts(db: Supabase, user_id: str, conversation_id: str, attachment_ids: list[str]) -> list[dict]:
    if len(attachment_ids) > 5 or len(set(attachment_ids)) != len(attachment_ids):
        raise HTTPException(422, "Envie no máximo cinco arquivos distintos por mensagem.")
    try:
        ids = [str(UUID(value)) for value in attachment_ids]
        conversation_id = str(UUID(conversation_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Identificador de arquivo inválido.") from None
    parts = []
    for attachment_id in ids:
        item = await _rpc(db, "get_attachment", p_user_id=user_id, p_attachment_id=attachment_id, p_conversation_id=conversation_id)
        metadata, data = item["attachment"], item["data_base64"].replace("\n", "")
        uri = f"data:{metadata['media_type']};base64,{data}"
        if metadata["media_type"] == "application/pdf":
            parts.append({"type": "input_file", "filename": metadata["filename"], "file_data": uri})
        else:
            parts.append({"type": "input_image", "image_url": uri, "detail": "auto"})
    return parts


class ShareCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_in_days: int = Field(default=7, ge=1, le=7, strict=True)


def _token_hash(token: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(404, "Compartilhamento não encontrado.", headers=PUBLIC_HEADERS)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@router.post("/conversations/{conversation_id}/shares", status_code=201)
async def create_share(conversation_id: UUID, request: Request, user_id: User, body: ShareCreate | None = None) -> dict:
    token = secrets.token_urlsafe(32)
    result = await _rpc(request.app.state.db, "create_share", p_user_id=user_id, p_conversation_id=str(conversation_id),
                        p_token_hash=_token_hash(token), p_expires_in_days=(body or ShareCreate()).expires_in_days)
    return {**result, "path": f"/share/{token}"}


@router.get("/conversations/{conversation_id}/shares")
async def list_shares(conversation_id: UUID, request: Request, user_id: User) -> dict:
    return await _rpc(request.app.state.db, "list_shares", p_user_id=user_id, p_conversation_id=str(conversation_id))


@router.delete("/shares/{share_id}")
async def revoke_share(share_id: UUID, request: Request, user_id: User) -> dict:
    return await _rpc(request.app.state.db, "revoke_share", p_user_id=user_id, p_share_id=str(share_id))


@router.get("/shared/{token}")
async def read_share(token: str, request: Request) -> JSONResponse:
    return JSONResponse(await _rpc(request.app.state.db, "read_share", p_token_hash=_token_hash(token)), headers=PUBLIC_HEADERS)


@router.get("/shared/{token}/attachments/{attachment_id}")
async def shared_attachment(token: str, attachment_id: UUID, request: Request) -> Response:
    # Never redirect to an independently usable blob URL: each GET checks expiry/revocation.
    return _binary(await _rpc(request.app.state.db, "read_shared_attachment", p_token_hash=_token_hash(token), p_attachment_id=str(attachment_id)))
