import { describe, expect, it } from "vitest";
import { DEFAULTS } from "../src/config";
import { costMicros } from "../src/metering";
import { bearer, call, fakeGoogle, jsonResponse, link, usage, usageRow } from "./helpers";

const GOOGLE_UPLOAD = "https://google.test/upload/v1beta/files?upload_id=UP123&upload_protocol=resumable";

function google() {
  return fakeGoogle((req) => {
    const url = new URL(req.url);
    if (url.pathname === "/upload/v1beta/files" && !url.searchParams.has("upload_id"))
      return new Response("{}", { headers: { "x-goog-upload-url": GOOGLE_UPLOAD, "x-goog-upload-status": "active" } });
    if (req.url === GOOGLE_UPLOAD)
      return jsonResponse({ file: { name: "files/abc", uri: "https://google.test/v1beta/files/abc", state: "ACTIVE" } }, 200, {
        "x-goog-upload-status": "final",
      });
    if (url.pathname === "/v1beta/files/abc") return req.method === "DELETE" ? jsonResponse({}) : jsonResponse({ name: "files/abc", state: "ACTIVE" });
    return jsonResponse({ error: { message: "unexpected" } }, 500);
  });
}

async function startUpload(token: string) {
  return call("/upload/v1beta/files", {
    method: "POST",
    headers: bearer(token, {
      "x-goog-upload-protocol": "resumable",
      "x-goog-upload-command": "start",
      "x-goog-upload-header-content-length": "5",
      "x-goog-upload-header-content-type": "audio/ogg",
    }),
    body: JSON.stringify({ file: { display_name: "audio" } }),
  });
}

describe("uploads", () => {
  it("rewrites Google's upload address to point back here, then streams the bytes through", async () => {
    const { token } = await link();
    const seen = google();
    const start = await startUpload(token);
    expect(start.status).toBe(200);
    expect(start.headers.get("x-goog-upload-url")).toBe("https://cloud.test/upload/v1beta/files?upload_id=UP123&upload_protocol=resumable");
    expect(start.headers.get("x-trailmix-level")).toBe("pro");
    expect(seen[0].url).toBe("https://google.test/upload/v1beta/files");
    expect(seen[0].headers.get("x-goog-upload-command")).toBe("start");
    expect(seen[0].headers.get("x-goog-upload-header-content-length")).toBe("5");
    expect(seen[0].headers.get("x-goog-api-key")).toBe("google-key");

    const target = new URL(start.headers.get("x-goog-upload-url")!);
    const done = await call(target.pathname + target.search, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "x-goog-upload-offset": "0", "x-goog-upload-command": "upload, finalize", "content-length": "5" },
      body: "OggS!",
    });
    expect(done.status).toBe(200);
    expect((await done.json<any>()).file.name).toBe("files/abc");
    expect(done.headers.get("x-goog-upload-status")).toBe("final");
    expect(seen[1].url).toBe(GOOGLE_UPLOAD);
    expect(seen[1].body).toBe("OggS!");
    expect(seen[1].headers.get("x-goog-upload-command")).toBe("upload, finalize");

    const got = await call("/v1beta/files/abc", { headers: bearer(token) });
    expect((await got.json<any>()).state).toBe("ACTIVE");
    expect((await call("/v1beta/files/abc", { method: "DELETE", headers: bearer(token) })).status).toBe(200);
    expect((await call("/v1beta/files/abc", { headers: bearer(token) })).status).toBe(404);
  });

  it("keeps one account out of another's uploads and files", async () => {
    const owner = await link();
    const other = await link();
    const seen = google();
    const start = await startUpload(owner.token);
    const target = new URL(start.headers.get("x-goog-upload-url")!);
    const stolen = await call(target.pathname + target.search, { method: "POST", headers: bearer(other.token), body: "x" });
    expect(stolen.status).toBe(404);
    await call(target.pathname + target.search, { method: "POST", headers: bearer(owner.token, { "content-length": "5" }), body: "OggS!" });
    expect((await call("/v1beta/files/abc", { headers: bearer(other.token) })).status).toBe(404);
    expect((await call("/v1beta/files/abc", { method: "DELETE", headers: bearer(other.token) })).status).toBe(404);
    expect(seen.filter((s) => s.url.includes("/v1beta/files/abc"))).toHaveLength(0);
  });
});

describe("batches", () => {
  it("records the job, keeps others out, and meters it once at the batch price", async () => {
    const { token, accountId } = await link();
    const other = await link();
    const results = [usage(0, 2000, 800), usage(0, 3000, 900)];
    const seen = fakeGoogle((req) => {
      if (req.url.endsWith(":batchGenerateContent")) return jsonResponse({ name: "batches/job1", metadata: { state: "BATCH_STATE_PENDING" } });
      return jsonResponse({
        name: "batches/job1",
        done: true,
        metadata: { state: "BATCH_STATE_SUCCEEDED" },
        response: { inlinedResponses: { inlinedResponses: results.map((u, i) => ({ metadata: { key: `k${i}` }, response: { usageMetadata: u } })) } },
      });
    });
    const created = await call("/v1beta/models/notes:batchGenerateContent", { method: "POST", headers: bearer(token), body: "{}" });
    expect((await created.json<any>()).name).toBe("batches/job1");
    expect(seen[0].url).toBe("https://google.test/v1beta/models/gemini-3.8-flash:batchGenerateContent");

    expect((await call("/v1beta/batches/job1", { headers: bearer(other.token) })).status).toBe(404);

    const job = await call("/v1beta/batches/job1", { headers: bearer(token) });
    expect(job.status).toBe(200);
    expect(job.headers.get("x-trailmix-level")).toBe("pro");
    await call("/v1beta/batches/job1", { headers: bearer(token) }); // polled again: not metered twice
    const row = await usageRow(accountId);
    expect(row.requests).toBe(2);
    expect(row.cost_micros).toBe(results.reduce((n, u) => n + costMicros(DEFAULTS, "gemini-3.8-flash", u, true), 0));
  });
});
