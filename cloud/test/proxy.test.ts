import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { DEFAULTS, modelFor } from "../src/config";
import { costMicros } from "../src/metering";
import {
  bearer,
  call,
  fakeGoogle,
  generate,
  jsonResponse,
  link,
  reply,
  subscribe,
  usage,
  usageRow,
} from "./helpers";

function modelIn(url: string) {
  return new URL(url).pathname.match(/models\/([^:]+):/)?.[1];
}

describe("role → model", () => {
  it("maps each role per plan and level", () => {
    expect(modelFor(DEFAULTS, "pro", "pro", "notes")).toBe("gemini-3.8-flash");
    expect(modelFor(DEFAULTS, "pro", "basic", "notes")).toBe("gemini-3.5-flash-lite");
    expect(modelFor(DEFAULTS, "basic", "basic", "transcribe")).toBe("gemini-3.5-flash-lite");
    expect(modelFor(DEFAULTS, "basic", "basic", "transcribe_chunk")).toBeNull();
    expect(modelFor(DEFAULTS, "trial", "pro", "transcribe_chunk")).toBe("gemini-3.5-flash-lite");
    const custom = { ...DEFAULTS, plans: { ...DEFAULTS.plans, max: { proHours: 150, models: { pro: { notes: "gemini-4-pro" } } } } };
    expect(modelFor(custom, "max", "pro", "notes")).toBe("gemini-4-pro");
    expect(modelFor(custom, "max", "pro", "quick")).toBe("gemini-3.5-flash-lite");
  });

  it("calls Google with the real model and our key, never the app's credentials", async () => {
    const { token } = await link();
    const seen = fakeGoogle(() => jsonResponse(reply()));
    const res = await call("/v1beta/models/notes:generateContent?key=sneaky", {
      method: "POST",
      headers: bearer(token),
      body: JSON.stringify({ contents: [] }),
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("x-trailmix-level")).toBe("pro");
    expect(res.headers.get("x-trailmix-model")).toBe("gemini-3.8-flash");
    expect(await res.json()).toEqual(reply());
    expect(seen).toHaveLength(1);
    expect(seen[0].url).toBe("https://google.test/v1beta/models/gemini-3.8-flash:generateContent");
    expect(seen[0].headers.get("x-goog-api-key")).toBe("google-key");
    expect(seen[0].headers.get("authorization")).toBeNull();
    expect(seen[0].body).toBe(JSON.stringify({ contents: [] }));
  });

  it("Basic gets flash-lite notes and no live captions", async () => {
    const { token, accountId } = await link();
    await subscribe(accountId, "basic");
    const seen = fakeGoogle(() => jsonResponse(reply()));
    const notes = await generate(token, "notes");
    expect(notes.headers.get("x-trailmix-level")).toBe("basic");
    expect(modelIn(seen[0].url)).toBe("gemini-3.5-flash-lite");
    const chunk = await generate(token, "transcribe_chunk");
    expect(chunk.status).toBe(409);
    const body = await chunk.json<any>();
    expect(body.level).toBe("basic");
    expect(body.error.message).toBeTruthy();
    expect(seen).toHaveLength(1);
  });

  it("refuses unknown roles and real model names", async () => {
    const { token } = await link();
    const seen = fakeGoogle(() => jsonResponse(reply()));
    expect((await generate(token, "gemini-3.8-flash")).status).toBe(404);
    expect((await generate(token, "nope")).status).toBe(404);
    expect(seen).toHaveLength(0);
  });

  it("hides Google refusing our key behind a 502", async () => {
    const { token } = await link();
    fakeGoogle(() => jsonResponse({ error: { code: 403, message: "API key invalid" } }, 403));
    const res = await generate(token, "quick");
    expect(res.status).toBe(502);
    expect(await res.text()).not.toContain("API key");
  });

  it("passes Google's rate limit through so the app waits", async () => {
    const { token } = await link();
    fakeGoogle(() => jsonResponse({ error: { code: 429, message: "slow down" } }, 429, { "retry-after": "8" }));
    const res = await generate(token, "quick");
    expect(res.status).toBe(429);
    expect(res.headers.get("retry-after")).toBe("8");
  });

  it("lists roles as models", async () => {
    const { token } = await link();
    const res = await call("/v1beta/models", { headers: bearer(token) });
    const names = (await res.json<any>()).models.map((m: any) => m.name);
    expect(names).toEqual(["models/transcribe", "models/transcribe_chunk", "models/notes", "models/quick"]);
  });
});

describe("metering", () => {
  it("counts actual cost and meeting hours", async () => {
    const { token, accountId } = await link();
    fakeGoogle(() => jsonResponse(reply(usage(1800, 1000, 3000))));
    await generate(token, "transcribe");
    const row = await usageRow(accountId);
    expect(row.pro_seconds).toBeCloseTo(1800);
    expect(row.requests).toBe(1);
    expect(row.cost_micros).toBe(costMicros(DEFAULTS, "gemini-3.5-flash-lite", usage(1800, 1000, 3000)));
    // 45,000 audio tokens + 1,000 text at $0.30/M, 3,000 out at $2.50/M
    expect(row.cost_micros).toBe(Math.round((46_000 * 0.3 + 3000 * 2.5)));
    const a = await (await call("/v1/account", { headers: bearer(token) })).json<any>();
    expect(a.trial_hours_left).toBe(4.5);
  });

  it("notes don't count as meeting hours", async () => {
    const { token, accountId } = await link();
    fakeGoogle(() => jsonResponse(reply(usage(600))));
    await generate(token, "notes");
    const row = await usageRow(accountId);
    expect(row.pro_seconds).toBe(0);
    expect(row.cost_micros).toBeGreaterThan(0);
  });

  it("a meeting counts as long as its longest stream, not the sum of its tracks", async () => {
    const { token, accountId } = await link();
    fakeGoogle(() => jsonResponse(reply(usage(15))));
    const meeting = { "x-trailmix-meeting": "m1" };
    for (let i = 0; i < 4; i++) {
      await generate(token, "transcribe_chunk", { ...meeting, "x-trailmix-track": "mic" });
      await generate(token, "transcribe_chunk", { ...meeting, "x-trailmix-track": "call" });
    }
    // A later speaker-names pass over the call track, shorter than the captions: nothing more.
    await generate(token, "transcribe", { ...meeting, "x-trailmix-track": "call" });
    let row = await usageRow(accountId);
    expect(row.pro_seconds).toBeCloseTo(60);
    expect(row.requests).toBe(9);

    // Without a meeting id, each request is a meeting of its own.
    await generate(token, "transcribe");
    row = await usageRow(accountId);
    expect(row.pro_seconds).toBeCloseTo(75);
  });

  it("counts the totals from the last event of a streamed reply", async () => {
    const { token, accountId } = await link();
    const events = [
      { candidates: [{ content: { parts: [{ text: "Hel" }] } }], usageMetadata: usage(0, 100, 1) },
      { candidates: [{ content: { parts: [{ text: "lo" }] } }], usageMetadata: usage(120, 100, 40) },
    ];
    const seen = fakeGoogle(
      () =>
        new Response(events.map((e) => `data: ${JSON.stringify(e)}\r\n\r\n`).join(""), {
          headers: { "content-type": "text/event-stream" },
        }),
    );
    const res = await call("/v1beta/models/transcribe:streamGenerateContent?alt=sse", {
      method: "POST",
      headers: bearer(token),
      body: "{}",
    });
    expect(seen[0].url).toBe("https://google.test/v1beta/models/gemini-3.5-flash-lite:streamGenerateContent?alt=sse");
    expect(await res.text()).toContain('"text":"lo"');
    const row = await usageRow(accountId);
    expect(row.pro_seconds).toBeCloseTo(120);
    expect(row.output_tokens).toBe(40);
  });

  it("past Pro's 50 hours, drops to the Basic level until the 1st", async () => {
    const { token, accountId } = await link();
    await subscribe(accountId, "pro");
    const seen = fakeGoogle(() => jsonResponse(reply(usage(49.5 * 3600))));
    const first = await generate(token, "transcribe_chunk");
    expect(first.headers.get("x-trailmix-level")).toBe("pro");
    let a = await (await call("/v1/account", { headers: bearer(token) })).json<any>();
    expect(a).toMatchObject({ level: "pro", pro_hours_used: 49.5, pro_hours_limit: 50 });

    await generate(token, "transcribe_chunk"); // another 49.5 h: now well past 50
    a = await (await call("/v1/account", { headers: bearer(token) })).json<any>();
    expect(a).toMatchObject({ plan: "pro", status: "active", level: "basic", pro_hours_used: 99 });
    expect(a.resets_at).toMatch(/-01T00:00:00.000Z$/);

    const chunk = await generate(token, "transcribe_chunk");
    expect(chunk.status).toBe(409);
    expect((await chunk.json<any>()).level).toBe("basic");
    const notes = await generate(token, "notes");
    expect(notes.status).toBe(200); // never 402 for running out of Pro hours
    expect(notes.headers.get("x-trailmix-level")).toBe("basic");
    expect(modelIn(seen.at(-1)!.url)).toBe("gemini-3.5-flash-lite");

    // Basic-level hours are counted apart and don't push the limit further.
    await generate(token, "transcribe");
    const row = await usageRow(accountId);
    expect(row.pro_seconds).toBeCloseTo(99 * 3600);
    expect(row.basic_seconds).toBeCloseTo(49.5 * 3600);
  });

  it("Max has 150 Pro-level hours", async () => {
    const { token, accountId } = await link();
    await subscribe(accountId, "max");
    fakeGoogle(() => jsonResponse(reply(usage(100 * 3600))));
    await generate(token, "transcribe");
    expect((await (await call("/v1/account", { headers: bearer(token) })).json<any>()).level).toBe("pro");
    await generate(token, "transcribe");
    expect((await (await call("/v1/account", { headers: bearer(token) })).json<any>()).level).toBe("basic");
  });

  it("the trial ends with 402 once its 5 hours are used", async () => {
    const { token, accountId } = await link();
    fakeGoogle(() => jsonResponse(reply(usage(5 * 3600))));
    expect((await generate(token, "transcribe")).status).toBe(200);
    const res = await generate(token, "notes");
    expect(res.status).toBe(402);
    const acct = await env.DB.prepare("SELECT trial_seconds FROM accounts WHERE id = ?").bind(accountId).first<any>();
    expect(acct.trial_seconds).toBeCloseTo(5 * 3600);
  });

  it("rate-limits each account", async () => {
    const { token } = await link();
    fakeGoogle(() => jsonResponse(reply()));
    const tight = { CLOUD_CONFIG: JSON.stringify({ rateLimitPerMinute: 2 }) };
    const send = () => call("/v1beta/models/quick:generateContent", { method: "POST", headers: bearer(token), body: "{}" }, tight);
    expect((await send()).status).toBe(200);
    expect((await send()).status).toBe(200);
    const third = await send();
    expect(third.status).toBe(429);
    expect(Number(third.headers.get("retry-after"))).toBeGreaterThan(0);
    // Another account isn't affected.
    const other = await link();
    expect((await call("/v1beta/models/quick:generateContent", { method: "POST", headers: bearer(other.token), body: "{}" }, tight)).status).toBe(200);
  });
});
