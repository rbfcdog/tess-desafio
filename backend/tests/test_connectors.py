"""Google connector behavior without real account credentials."""

import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

from services import connectors


class GoogleContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_gmail_body_preserves_charset_and_excludes_file_attachments(self):
        inline = "Olá; próximo passo."
        html = "<p>Reunião às três.</p>"
        encode = lambda data: base64.urlsafe_b64encode(data).decode().rstrip("=")
        message_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/mail_1"
        message = {"id": "mail_1", "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Reunião"}],
            "parts": [
                {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=unknown-charset"}],
                 "body": {"data": encode(inline.encode())}},
                {"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/html", "headers": [{"name": "Content-Type", "value": "text/html; charset=iso-8859-1"}],
                     "body": {"attachmentId": "external/body?="}},
                ]},
                {"mimeType": "text/plain", "filename": "private.txt", "body": {"data": encode(b"not the message body")}},
            ],
        }}

        async def provider(method, url, **kwargs):
            if method == "GET" and url == message_url:
                value = message
            elif method == "GET" and url == message_url + "/attachments/external%2Fbody%3F%3D":
                value = {"data": encode(html.encode("iso-8859-1"))}
            else:
                raise AssertionError("Unexpected Google request")
            return json.dumps(value).encode(), "application/json", url

        with patch.object(connectors, "access_token", AsyncMock(return_value="test-access-token")), patch.object(connectors, "request_bytes", provider):
            result = await connectors.execute_google(None, "owner", "gmail_read", {"message_id": "mail_1"})
        self.assertEqual(result["bodies"], [{"media_type": "text/plain", "text": inline}, {"media_type": "text/html", "text": html}])


if __name__ == "__main__":
    unittest.main()
