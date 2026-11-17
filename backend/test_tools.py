"""Focused security checks; no real external services or credentials required."""

import asyncio
import json
import os
import socket
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpcore2
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request

import connectors
import tools
from safe_http import MAX_RESPONSE_BYTES, LimitedStream, PinnedBackend, SafeTransport, UnsafeURL, public_ip, validate_url


class FakeTLSStream(httpcore2.AsyncNetworkStream):
    def __init__(self):
        self.host = None
        self.context = None
        self.written = b""
        self.reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"

    async def read(self, max_bytes, timeout=None):
        reply, self.reply = self.reply, b""
        return reply

    async def write(self, buffer, timeout=None):
        self.written += buffer

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.host, self.context = server_hostname, ssl_context
        return self

    def get_extra_info(self, info):
        return False if info == "is_readable" else None


class ToolBoundaries(unittest.IsolatedAsyncioTestCase):
    def test_public_url_and_address_boundaries(self):
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com:8443", "https://localhost/", "https://a.local/", "https://example.com/#x", "https://example.com\\@127.0.0.1"):
            with self.subTest(url=url), self.assertRaises(UnsafeURL):
                validate_url(url)
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::", "224.0.0.1"):
            self.assertFalse(public_ip(address), address)
        self.assertTrue(public_ip("1.1.1.1"))

    async def test_ip_pinning_retains_original_sni_and_certificate_checks(self):
        stream = FakeTLSStream()
        dialer = SimpleNamespace(connect_tcp=AsyncMock(return_value=stream))
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)):
            async with httpcore2.AsyncConnectionPool(network_backend=PinnedBackend(dialer)) as pool:
                response = await pool.request("GET", "https://public.example/read")
                self.assertEqual(response.content, b"ok")
        self.assertEqual(dialer.connect_tcp.call_args.args, ("1.1.1.1", 443))
        self.assertEqual(stream.host, "public.example")
        self.assertTrue(stream.context.check_hostname)
        self.assertEqual(stream.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIn(b"Host: public.example", stream.written)

    async def test_mixed_dns_and_rebinding_are_rejected_before_second_dial(self):
        dialer = SimpleNamespace(connect_tcp=AsyncMock(return_value=object()))
        loop = asyncio.get_running_loop()
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        backend = PinnedBackend(dialer)
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=[public, private, public + private])):
            await backend.connect_tcp("public.example", 443)
            with self.assertRaises(UnsafeURL):
                await backend.connect_tcp("public.example", 443)
            with self.assertRaises(UnsafeURL):
                await backend.connect_tcp("mixed.example", 443)
        self.assertEqual(dialer.connect_tcp.await_count, 1)

    async def test_transport_caps_stream_and_rejects_cross_origin_before_dial(self):
        import httpx2
        transport = SafeTransport(origin="https://trusted.example/mcp")
        try:
            with self.assertRaises(UnsafeURL):
                await transport.handle_async_request(httpx2.Request("GET", "https://other.example/mcp"))
            class Oversized(httpx2.AsyncByteStream):
                async def __aiter__(self):
                    yield b"a" * MAX_RESPONSE_BYTES
                    yield b"b"
            stream = LimitedStream(Oversized(), transport)
            with self.assertRaises(UnsafeURL):
                async for _ in stream:
                    pass
        finally:
            await transport.aclose()

    def test_external_schema_references_and_regex_are_rejected(self):
        for schema in ({"type": "object", "properties": {"x": {"$ref": "http://169.254.169.254/"}}},
                       {"type": "object", "properties": {"x": {"pattern": "(a+)+$"}}}):
            with self.assertRaises(ValueError):
                tools.validate_schema(schema)
        tools.validate_schema({"type": "object", "properties": {"q": {"type": "string", "maxLength": 100}}})

    async def test_mcp_output_schema_cannot_trigger_unguarded_reference_fetch(self):
        schema = {"type": "object", "properties": {}}
        remote = SimpleNamespace(name="read", input_schema=schema,
                                 output_schema={"type": "object", "$ref": "http://169.254.169.254/"},
                                 annotations=SimpleNamespace(read_only_hint=True, destructive_hint=False))
        sdk = SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[remote], next_cursor=None)),
                              session=SimpleNamespace(call_tool=AsyncMock()))
        context = MagicMock()
        context.__aenter__.return_value = sdk
        row = {"config": {"url": "https://readonly.example/mcp", "allowed_tools": ["read"],
                          "discovered_tools": [{"name": "read", "input_schema": schema}]}}
        with patch.dict(os.environ, {"MCP_TRUSTED_HOSTS": "readonly.example"}), patch.object(tools, "Client", return_value=context):
            with self.assertRaises(ValueError):
                await tools.mcp_operation(row, "read", {})
        sdk.session.call_tool.assert_not_awaited()

    def test_secrets_are_redacted_and_truncation_preserves_json_bounds(self):
        value = tools.sanitize({"access_token": "a", "echo": "bearer-real-secret", "nextPageToken": "next", "body": "é" * 50000}, ["bearer-real-secret"])
        encoded = json.dumps(value, ensure_ascii=False).encode()
        self.assertNotIn(b"bearer-real-secret", encoded)
        self.assertTrue(value["truncated"])
        self.assertLess(len(encoded), 32768)
        self.assertEqual(tools.sanitize({"nextPageToken": "next"})["nextPageToken"], "next")

    async def test_invalid_arguments_are_audited_without_network_execution(self):
        db = SimpleNamespace(rpc=AsyncMock(return_value={"id": "execution"}))
        registry = tools.ToolRegistry(db, "owner", "conversation", "run")
        registry.add("read_webpage", "read", tools.PAGE_SCHEMA, {}, "page")
        with patch.object(tools, "read_webpage", AsyncMock()) as external:
            result = await registry.execute("read_webpage", '{"url": 7}')
        external.assert_not_awaited()
        self.assertIn("error", result)
        self.assertEqual(db.rpc.await_args_list[0].args[0], "start_tool_execution")
        self.assertEqual(db.rpc.await_args_list[-1].kwargs["p_status"], "failed")

    async def test_disabling_tool_after_registry_load_prevents_external_execution(self):
        original = {"id": "tool-id", "name": "public_read", "kind": "http", "enabled": True,
                    "config": {"url": "https://example.com"}, "secret": None}
        db = SimpleNamespace(rpc=AsyncMock(return_value={"id": "execution"}),
                             rows=AsyncMock(return_value=[{**original, "enabled": False}]))
        registry = tools.ToolRegistry(db, "owner", "conversation", "run")
        registry.add("public_read", "read", {"type": "object", "properties": {}}, original, "http")
        with patch.object(tools, "request_bytes", AsyncMock()) as external:
            result = await registry.execute("public_read", "{}")
        external.assert_not_awaited()
        self.assertIn("error", result)
        self.assertEqual(db.rpc.await_args_list[-1].kwargs["p_status"], "failed")

    async def test_cancellation_closes_execution_before_propagating(self):
        db = SimpleNamespace(rpc=AsyncMock(return_value={"id": "execution"}))
        registry = tools.ToolRegistry(db, "owner", "conversation", "run")
        registry.add("read_webpage", "read", tools.PAGE_SCHEMA, {}, "page")
        started = asyncio.Event()
        async def waiting(url):
            started.set()
            await asyncio.Future()
        with patch.object(tools, "read_webpage", waiting):
            task = asyncio.create_task(registry.execute("read_webpage", '{"url":"https://example.com"}'))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(db.rpc.await_args_list[-1].kwargs["p_status"], "failed")

    async def test_oauth_state_requires_initiating_browser_cookie(self):
        env = {"GOOGLE_CLIENT_ID": "client", "GOOGLE_CLIENT_SECRET": "secret", "GOOGLE_REDIRECT_URI": "http://localhost/api/connectors/google/callback", "PUBLIC_APP_URL": "http://localhost", "ENCRYPTION_KEY": Fernet.generate_key().decode()}
        db = SimpleNamespace(rpc=AsyncMock())
        request = Request({"type": "http", "headers": [], "app": SimpleNamespace(state=SimpleNamespace(db=db))})
        with patch.dict(os.environ, env):
            with self.assertRaises(HTTPException) as error:
                await connectors.google_callback(request, state="a" * 43, code="code")
            self.assertEqual(error.exception.status_code, 400)
        db.rpc.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
