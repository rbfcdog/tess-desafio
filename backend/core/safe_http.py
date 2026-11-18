"""Bounded public-HTTPS transport. DNS is checked at the actual socket dial."""

import asyncio
import ipaddress
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

import httpcore2
import httpx2

MAX_RESPONSE_BYTES = 1024 * 1024


class UnsafeURL(ValueError):
    pass


def validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise UnsafeURL("URL must be a string.")
    try:
        value = urlsplit(url)
        host = (value.hostname or "").rstrip(".").lower().encode("idna").decode()
        if (value.scheme != "https" or not host or value.username or value.password
                or value.fragment or value.port not in (None, 443) or len(url) > 4096
                or any(ord(c) < 33 for c in url) or "\\" in url or "%" in host
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError
    except (ValueError, UnicodeError):
        raise UnsafeURL("Use a public HTTPS URL on port 443, without credentials or fragments.") from None
    return host


def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return public_ip(str(address.ipv4_mapped))
        # Translation/tunnel prefixes can otherwise reach nonpublic IPv4 destinations.
        if address.sixtofour is not None or address.teredo is not None:
            return False
        if any(address in ipaddress.ip_network(net) for net in ("64:ff9b::/96", "64:ff9b:1::/48")):
            return False
    return True


class PinnedBackend(httpcore2.AsyncNetworkBackend):
    def __init__(self, backend: Any = None):
        self.backend = backend if backend is not None else httpcore2.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None, socket_options: Any = None) -> Any:
        if port != 443:
            raise UnsafeURL("Only HTTPS port 443 is permitted.")
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(min(timeout or 5, 5)):
            answers = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or any(not public_ip(address) for address in addresses):
            raise UnsafeURL("Private, local, reserved or mixed DNS destinations are forbidden.")
        # Only a vetted numeric address reaches the socket backend. httpcore2 retains
        # the original origin hostname for Host, TLS SNI and certificate validation.
        return await self.backend.connect_tcp(
            addresses[0], port, timeout=timeout, local_address=None, socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise UnsafeURL("Unix sockets are forbidden.")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class LimitedStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream, transport: "SafeTransport"):
        self.stream, self.transport = stream, transport
        self.size = 0

    async def __aiter__(self):
        async for chunk in self.stream:
            self.size += len(chunk)
            self.transport.remaining -= len(chunk)
            if self.size > MAX_RESPONSE_BYTES or self.transport.remaining < 0:
                raise UnsafeURL("External response exceeds the byte limit.")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class SafeTransport(httpx2.AsyncHTTPTransport):
    def __init__(self, *, origin: str | None = None, total_bytes: int = 2 * MAX_RESPONSE_BYTES):
        super().__init__(verify=True, trust_env=False, retries=0)
        # httpx2 does not expose network_backend; this single version-pinned bridge
        # keeps httpcore2's original-origin TLS machinery, rather than rewriting URLs.
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=PinnedBackend(),
            max_connections=4, max_keepalive_connections=0, retries=0,
        )
        self.origin = httpx2.URL(origin).copy_with(path="/", query=None) if origin else None
        self.remaining = total_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        validate_url(str(request.url))
        if self.origin and (request.url.scheme, request.url.host, request.url.port) != (
                self.origin.scheme, self.origin.host, self.origin.port):
            raise UnsafeURL("Cross-origin requests are forbidden.")
        request.headers["accept-encoding"] = "identity"
        response = await super().handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() not in ("identity", ""):
            await response.aclose()
            raise UnsafeURL("Compressed external responses are forbidden.")
        length = response.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            await response.aclose()
            raise UnsafeURL("External response exceeds the byte limit.")
        response.stream = LimitedStream(response.stream, self)
        return response


def safe_client(*, origin: str | None = None, headers: dict | None = None) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=SafeTransport(origin=origin), trust_env=False, follow_redirects=False, max_redirects=0,
        timeout=httpx2.Timeout(15, connect=5, pool=5), headers=headers,
    )


async def request_bytes(method: str, url: str, *, headers: dict | None = None,
                        params: dict | None = None, data: dict | None = None,
                        redirects: int = 0) -> tuple[bytes, str, str]:
    """GET redirects are opt-in, bounded and never carry cross-origin secrets."""
    validate_url(url)
    async with asyncio.timeout(30):
        async with safe_client(headers=headers) as client:
            current = httpx2.URL(url)
            for hop in range(redirects + 1):
                async with client.stream(method, current, params=params, data=data) as response:
                    if response.is_redirect:
                        if method != "GET" or hop >= redirects or not response.headers.get("location"):
                            raise UnsafeURL("External redirect is not permitted.")
                        target = current.join(response.headers["location"])
                        validate_url(str(target))
                        if (target.scheme, target.host, target.port) != (current.scheme, current.host, current.port):
                            # No headers/cookies from a previous origin, even when supplied by a server.
                            client.headers.pop("authorization", None)
                        client.cookies.clear()
                        current, params = target, None
                        continue
                    if response.is_error:
                        raise UnsafeURL(f"External service returned HTTP {response.status_code}.")
                    content = await response.aread()
                    return content, response.headers.get("content-type", ""), str(response.url)
    raise UnsafeURL("External request did not complete.")
