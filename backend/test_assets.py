import base64
import io
import os
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from PIL import Image
from pypdf import PdfReader, PdfWriter
from starlette.requests import Request

from assets import MAX_FILE_BYTES, _binary, model_parts, normalize_file, shared_attachment, upload_attachment


def png() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (8, 4), "red")
    exif = Image.Exif()
    exif[270] = "private metadata"
    image.save(output, format="PNG", exif=exif)
    return output.getvalue()


def pdf(pages: int = 1, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Author": "private metadata"})
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class FileBoundaryTests(unittest.TestCase):
    def test_images_are_reencoded_resized_and_metadata_removed(self):
        output = io.BytesIO()
        Image.new("RGB", (4096, 20), "red").save(output, format="JPEG", exif=Image.Exif())
        _, media, normalized, pages = normalize_file(output.getvalue(), "photo.jpg", "image/jpeg")
        with Image.open(io.BytesIO(normalized)) as image:
            self.assertEqual(image.size, (2048, 10))
            self.assertFalse(image.getexif())
        _, _, normalized, _ = normalize_file(png(), "photo.png", "image/png")
        with Image.open(io.BytesIO(normalized)) as image:
            self.assertFalse(image.getexif())
        self.assertEqual(media, "image/jpeg")
        self.assertIsNone(pages)

    def test_rejects_mime_mismatch_paths_active_images_and_oversize(self):
        for data, name, mime in [
            (png(), "wrong.jpg", "image/jpeg"),
            (png(), "../photo.png", "image/png"),
            (png(), "x\r\ny.png", "image/png"),
            (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "x.png", "image/png"),
            (b"<html>not an image</html>", "x.png", "image/png"),
            (png(), "x.png", "text/html"),
            (b"x" * (MAX_FILE_BYTES + 1), "x.pdf", "application/pdf"),
        ]:
            with self.subTest(name=name, mime=mime), self.assertRaises(HTTPException):
                normalize_file(data, name, mime)

    def test_image_decoding_limit_is_enforced_before_load(self):
        with patch("assets.MAX_IMAGE_PIXELS", 4), self.assertRaises(HTTPException) as error:
            normalize_file(png(), "large.png", "image/png")
        self.assertEqual(error.exception.status_code, 422)

    def test_pdf_pages_encryption_signature_and_metadata(self):
        _, _, normalized, pages = normalize_file(pdf(), "report.pdf", "application/pdf")
        self.assertEqual(pages, 1)
        self.assertNotIn("private metadata", str(PdfReader(io.BytesIO(normalized)).metadata))
        for data in (pdf(21), pdf(password="password"), b"%PDF-1.7\ninvalid\n%%EOF", b"<html>PDF</html>"):
            with self.subTest(size=len(data)), self.assertRaises(HTTPException):
                normalize_file(data, "report.pdf", "application/pdf")

    def test_pdf_compressed_stream_limit(self):
        from pypdf.generic import DecodedStreamObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        stream = DecodedStreamObject()
        stream.set_data(b" " * (21 * 1024 * 1024))
        page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaises(HTTPException):
            normalize_file(output.getvalue(), "bomb.pdf", "application/pdf")

    def test_download_headers_cannot_be_injected(self):
        response = _binary({"attachment": {"filename": 'private\";x=1.pdf', "media_type": "application/pdf"},
                            "data_base64": base64.b64encode(pdf()).decode()})
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
        self.assertIn("%22", response.headers["content-disposition"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "no-store")


class AttachmentAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_input_parts_and_attachment_limits(self):
        owner, conversation, attachment = str(uuid4()), str(uuid4()), str(uuid4())
        db = SimpleNamespace(rpc=AsyncMock(return_value={"attachment": {"media_type": "image/png", "filename": "x.png"},
                                                        "data_base64": base64.b64encode(png()).decode()}))
        parts = await model_parts(db, owner, conversation, [attachment])
        self.assertTrue(parts[0]["image_url"].startswith("data:image/png;base64,"))
        for ids in ([attachment] * 2, [str(uuid4()) for _ in range(6)]):
            with self.assertRaises(HTTPException):
                await model_parts(db, owner, conversation, ids)
        db.rpc.return_value = {"error": "ATTACHMENT_NOT_FOUND"}
        with self.assertRaises(HTTPException) as error:
            await model_parts(db, owner, conversation, [attachment])
        self.assertEqual(error.exception.status_code, 404)


    async def test_real_multipart_upload_stores_only_normalized_bytes(self):
        body = (b'--test\r\nContent-Disposition: form-data; name="file"; filename="photo.png"\r\n'
                b'Content-Type: image/png\r\n\r\n' + png() + b"\r\n--test--\r\n")

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        conversation, owner = uuid4(), str(uuid4())
        db = SimpleNamespace(owned_conversation=AsyncMock(), rpc=AsyncMock(return_value={"attachment": {"id": str(uuid4())}}))
        app = SimpleNamespace(state=SimpleNamespace(db=db))
        request = Request({"type": "http", "method": "POST", "path": "/", "app": app,
                           "headers": [(b"content-type", b"multipart/form-data; boundary=test")]}, receive)
        await upload_attachment(conversation, request, owner)
        saved = db.rpc.await_args
        with Image.open(io.BytesIO(base64.b64decode(saved.kwargs["p_data_base64"]))) as image:
            self.assertFalse(image.getexif())

    async def test_upload_is_bounded_before_multipart_spooling(self):
        async def receive():
            return {"type": "http.request", "body": b"x" * (MAX_FILE_BYTES + 65537), "more_body": False}

        db = SimpleNamespace(owned_conversation=AsyncMock(), rpc=AsyncMock())
        app = SimpleNamespace(state=SimpleNamespace(db=db))
        request = Request({"type": "http", "method": "POST", "path": "/", "app": app,
                           "headers": [(b"content-type", b"multipart/form-data; boundary=test")]}, receive)
        with self.assertRaises(HTTPException) as error:
            await upload_attachment(uuid4(), request, str(uuid4()))
        self.assertEqual(error.exception.status_code, 413)
        db.rpc.assert_not_awaited()


# Run against a disposable migrated Supabase database as postgres. All fixture rows roll back.
DATABASE_SECURITY_CHECK = r"""
begin;
do $$
declare
  u1 uuid := gen_random_uuid(); u2 uuid := gen_random_uuid();
  c1 uuid; c2 uuid; a1 uuid; a2 uuid; r1 uuid := gen_random_uuid(); m1 uuid := gen_random_uuid();
  share_id uuid; snapshot jsonb; result jsonb;
begin
  insert into auth.users(id,email) values (u1,u1::text||'@example.invalid'),(u2,u2::text||'@example.invalid');
  c1 := (public.create_conversation(u1,'Shared title')->>'id')::uuid;
  c2 := (public.create_conversation(u2,'Foreign title')->>'id')::uuid;
  result := public.store_attachment(u1,c1,'photo.png','image/png','__PNG_BASE64__',null);
  a1 := (result->'attachment'->>'id')::uuid;
  if a1 is null then raise exception 'Upload did not persist'; end if;
  if public.get_attachment(u2,a1)->>'error' is distinct from 'ATTACHMENT_NOT_FOUND' then raise exception 'Foreign blob disclosed'; end if;
  if public.get_attachment(u1,a1,c2)->>'error' is distinct from 'ATTACHMENT_NOT_FOUND' then raise exception 'Cross-conversation blob disclosed'; end if;
  if public.store_attachment(u1,c2,'photo.png','image/png','__PNG_BASE64__',null)->>'error' is distinct from 'CONVERSATION_NOT_FOUND' then raise exception 'Foreign upload allowed'; end if;
  insert into public.runs(id,user_id,conversation_id,request_id,model,reserved_tokens) values (r1,u1,c1,gen_random_uuid(),'test',1);
  insert into public.messages(id,user_id,conversation_id,run_id,role,content,status) values (m1,u1,c1,r1,'user','snapshot text','completed');
  perform public.bind_run_attachments(u1,r1,array[a1]);
  perform public.bind_run_attachments(u1,r1,array[a1]);
  if (select count(*) from public.message_attachments where message_id=m1)<>1 then raise exception 'Binding not idempotent'; end if;
  if public.remove_attachment(u1,a1)->>'error' is distinct from 'ATTACHMENT_BOUND' then raise exception 'Bound file deleted'; end if;
  begin
    perform public.bind_run_attachments(u2,r1,array[a1]);
    raise exception 'Foreign binding allowed';
  exception when raise_exception then
    if sqlerrm<>'RUN_NOT_FOUND' then raise; end if;
  end;
  update public.user_settings set system_prompt='PRIVATE_SYSTEM_SECRET' where user_id=u1;
  insert into public.tool_executions(user_id,conversation_id,run_id,name,status,arguments,result)
    values (u1,c1,r1,'read_webpage','completed','{}','{"text":"snapshot tool result"}');
  result := public.create_share(u1,c1,repeat('a',64),7);
  share_id := (result->'share'->>'id')::uuid;
  if share_id is null then raise exception 'Share did not persist'; end if;
  snapshot := public.read_share(repeat('a',64));
  if snapshot::text like '%PRIVATE_SYSTEM_SECRET%' then raise exception 'System prompt disclosed'; end if;
  if jsonb_array_length(snapshot->'messages')<>1 or jsonb_array_length(snapshot->'tool_calls')<>1 then raise exception 'Incomplete snapshot'; end if;
  if public.list_shares(u1,c1)::text like '%token_hash%' then raise exception 'Token hash disclosed'; end if;
  update public.messages set content='future content' where id=m1;
  update public.tool_executions set result='{"text":"future tool result"}' where run_id=r1;
  insert into public.messages(user_id,conversation_id,run_id,role,content,status) values (u1,c1,r1,'assistant','future message','running');
  if public.read_share(repeat('a',64))<>snapshot then raise exception 'Snapshot changed after creation'; end if;
  if public.read_shared_attachment(repeat('a',64),a1)->>'data_base64' is null then raise exception 'Snapshot file missing'; end if;
  a2 := (public.store_attachment(u1,c1,'later.png','image/png','__PNG_BASE64__',null)->'attachment'->>'id')::uuid;
  if public.read_shared_attachment(repeat('a',64),a2)->>'error' is distinct from 'SHARE_NOT_FOUND' then raise exception 'Unsnapped file disclosed'; end if;
  if public.revoke_share(u2,share_id)->>'error' is distinct from 'SHARE_NOT_FOUND' then raise exception 'Foreign revoke allowed'; end if;
  perform public.revoke_share(u1,share_id);
  if public.read_share(repeat('a',64))->>'error' is distinct from 'SHARE_NOT_FOUND' then raise exception 'Revoked share disclosed'; end if;
  if public.read_shared_attachment(repeat('a',64),a1)->>'error' is distinct from 'SHARE_NOT_FOUND' then raise exception 'Revoked file disclosed'; end if;
  result := public.create_share(u1,c1,repeat('b',64),1);
  update public.conversation_shares set created_at=clock_timestamp()-interval '2 days',expires_at=clock_timestamp()-interval '1 day'
    where id=(result->'share'->>'id')::uuid;
  if public.read_share(repeat('b',64))->>'error' is distinct from 'SHARE_NOT_FOUND' then raise exception 'Expired share disclosed'; end if;
  if public.read_shared_attachment(repeat('b',64),a1)->>'error' is distinct from 'SHARE_NOT_FOUND' then raise exception 'Expired file disclosed'; end if;
  insert into public.attachments(user_id,conversation_id,filename,media_type,size_bytes)
    select u2,c2,'quota.png','image/png',5242880 from generate_series(1,10);
  if public.store_attachment(u2,c2,'photo.png','image/png','__PNG_BASE64__',null)->>'error' is distinct from 'ATTACHMENT_QUOTA_EXCEEDED' then raise exception 'Account quota exceeded'; end if;
  if has_table_privilege('authenticated','public.attachment_blobs','SELECT') or has_table_privilege('service_role','public.attachment_blobs','SELECT') then raise exception 'Direct blob reads granted'; end if;
  if has_function_privilege('anon','public.read_share(text)','EXECUTE') or has_function_privilege('authenticated','public.get_attachment(uuid,uuid,uuid)','EXECUTE') then raise exception 'Trusted RPC exposed to browser'; end if;
end;
$$;
rollback;
"""


@unittest.skipUnless(os.environ.get("ASSETS_TEST_DATABASE_URL") and shutil.which("psql"), "Set ASSETS_TEST_DATABASE_URL and install psql for real database security checks")
class DatabaseSecurityTests(unittest.TestCase):
    def test_ownership_quota_snapshot_membership_revocation_and_expiry(self):
        sql = DATABASE_SECURITY_CHECK.replace("__PNG_BASE64__", base64.b64encode(png()).decode())
        result = subprocess.run(["psql", os.environ["ASSETS_TEST_DATABASE_URL"], "-X", "-v", "ON_ERROR_STOP=1"],
                                input=sql, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
