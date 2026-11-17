import assert from "node:assert/strict";
import test from "node:test";
import { apiFetch, ApiError } from "./chat-api.ts";
import { maxAttachmentBytes, retainUnsentAttachments, validateUploads } from "./attachments.ts";

const image = { name: "foto.png", type: "image/png", size: 100 };

test("uploads enforce the shared type, size and per-message boundaries", () => {
  assert.equal(validateUploads([image, { name: "texto.pdf", type: "application/pdf", size: maxAttachmentBytes }], 3), null);
  assert.match(validateUploads([image], 5), /5 arquivos/);
  assert.match(validateUploads([{ ...image, size: maxAttachmentBytes + 1 }], 0), /5 MB/);
  assert.match(validateUploads([{ ...image, size: 0 }], 0), /1 byte/);
  for (const type of ["image/svg+xml", "text/html", ""]) assert.match(validateUploads([{ ...image, type }], 0), /PNG, JPEG, WebP ou PDF/);
});

test("history synchronization keeps unsent attachments and removes attachments accepted without a start event", () => {
  const first = { id: "first" }, second = { id: "second" };
  const selected = [first, second];
  assert.deepEqual(retainUnsentAttachments(selected, [{ role: "assistant", content: "Earlier reply" }]), selected);
  assert.deepEqual(retainUnsentAttachments(selected, [{ role: "user", attachments: [{ id: "first" }] }]), [second]);
  assert.deepEqual(selected, [first, second]);
});

test("attachment multipart and explicit deletion preserve auth without leaking tokens to URLs", async (t) => {
  const requests = [];
  const client = { auth: { getSession: async () => ({ data: { session: { access_token: "private-access-token" } }, error: null }) } };
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, options });
    return Response.json({ ok: true });
  });
  const form = new FormData();
  form.append("file", new Blob(["bytes"], { type: "image/png" }), "foto.png");
  await apiFetch(client, "/conversations/owned/attachments", new AbortController().signal, form);
  await apiFetch(client, "/attachments/file", new AbortController().signal, undefined, "DELETE");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(requests[0].options.body, form);
  assert.equal(requests[0].options.headers["Content-Type"], undefined);
  assert.equal(requests[1].options.method, "DELETE");
  for (const { url, options } of requests) {
    assert.equal(url.includes("private-access-token"), false);
    assert.equal(options.headers.Authorization, "Bearer private-access-token");
    assert.equal(options.cache, "no-store");
  }
});

test("missing sessions and aborted requests cannot access private feature routes", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => Response.json({}));
  const missing = { auth: { getSession: async () => ({ data: { session: null }, error: null }) } };
  await assert.rejects(apiFetch(missing, "/tools", new AbortController().signal), (error) => error instanceof ApiError && error.status === 401);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(apiFetch(missing, "/tools", controller.signal), { name: "AbortError" });
  assert.equal(fetch.mock.callCount(), 0);
});
