import { createExecutionContext, env, waitOnExecutionContext } from "cloudflare:test";
import { vi } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/env";
import { hmacSha256 } from "../src/util";

export const BASE = "https://cloud.test";
export const ORIGIN = { origin: BASE };

/** Calls the Worker and waits for its background work (metering) to finish, so tests can check the database. */
export async function call(path: string, init: RequestInit = {}, envOverride: Partial<Env> = {}): Promise<Response> {
  const ctx = createExecutionContext();
  const res = await worker.fetch(new Request(BASE + path, init), { ...env, ...envOverride } as Env, ctx);
  const body = [101, 204, 205, 304].includes(res.status) ? null : await res.arrayBuffer();
  await waitOnExecutionContext(ctx);
  return new Response(body, { status: res.status, headers: res.headers });
}

export function bearer(token: string, extra: Record<string, string> = {}): Record<string, string> {
  return { authorization: `Bearer ${token}`, "content-type": "application/json", ...extra };
}

let n = 0;
export function unique(prefix = "user"): string {
  return `${prefix}-${Date.now()}-${++n}`;
}

/** Signs in through the emailed link (printed to the console in DEV_MODE) and returns the session cookie. */
export async function signIn(email: string): Promise<string> {
  const log = vi.spyOn(console, "log").mockImplementation(() => {});
  const res = await call("/auth/email", { method: "POST", headers: ORIGIN, body: new URLSearchParams({ email, next: "/account" }) });
  if (res.status !== 200) throw new Error(`email link: ${res.status}`);
  const line = String(log.mock.calls.map((c) => c.join(" ")).find((l) => l.includes(email)));
  log.mockRestore();
  const token = new URL(line.slice(line.indexOf("https://"))).searchParams.get("token")!;
  const verified = await call("/auth/verify", { method: "POST", headers: ORIGIN, body: new URLSearchParams({ token }) });
  if (verified.status !== 303) throw new Error(`verify: ${verified.status}`);
  return verified.headers.get("set-cookie")!.split(";")[0];
}

/** The whole linking flow: the app asks for a code, the person approves it, the app collects its token. */
export async function link(email = `${unique()}@example.com`, deviceId: string | null = unique("mac")) {
  const start = await (
    await call("/v1/link", { method: "POST", body: JSON.stringify({ device_name: "Test Mac", ...(deviceId ? { device_id: deviceId } : {}) }) })
  ).json<any>();
  const cookie = await signIn(email);
  const approved = await call("/link/approve", {
    method: "POST",
    headers: { ...ORIGIN, cookie },
    body: new URLSearchParams({ code: start.code }),
  });
  if (approved.status !== 200) throw new Error(`approve: ${approved.status}`);
  const poll = await (await call("/v1/link/poll", { method: "POST", body: JSON.stringify({ code: start.code }) })).json<any>();
  const account = await env.DB.prepare("SELECT * FROM accounts WHERE email = ?").bind(email).first<any>();
  return { token: poll.token as string, email, cookie, accountId: account.id as string };
}

/** Sends a signed Lemon Squeezy subscription event. */
export async function webhook(attrs: Record<string, unknown>, opts: { accountId?: string; event?: string; secret?: string } = {}) {
  const body = JSON.stringify({
    meta: { event_name: opts.event ?? "subscription_updated", custom_data: opts.accountId ? { account_id: opts.accountId } : undefined },
    data: {
      id: "sub_1",
      type: "subscriptions",
      attributes: { status: "active", customer_id: 7, urls: { customer_portal: "https://shop.test/portal" }, ...attrs },
    },
  });
  return call("/webhooks/lemonsqueezy", {
    method: "POST",
    headers: { "x-signature": await hmacSha256(opts.secret ?? "ls-secret", body), "content-type": "application/json" },
    body,
  });
}

export async function subscribe(accountId: string, plan: "basic" | "pro" | "max", status = "active") {
  const variant = { basic: 101, pro: 102, max: 103 }[plan];
  const res = await webhook({ variant_id: variant, status, renews_at: "2099-01-01T00:00:00Z" }, { accountId });
  if (res.status !== 200) throw new Error(`webhook: ${res.status} ${await res.text()}`);
}

/** A response's usage: `audioSeconds` of audio at 25 tokens a second, plus some text in and out. */
export function usage(audioSeconds = 0, textIn = 1000, out = 500) {
  const audio = Math.round(audioSeconds * 25);
  return {
    promptTokenCount: audio + textIn,
    candidatesTokenCount: out,
    totalTokenCount: audio + textIn + out,
    promptTokensDetails: [
      { modality: "TEXT", tokenCount: textIn },
      ...(audio ? [{ modality: "AUDIO", tokenCount: audio }] : []),
    ],
  };
}

export function reply(u = usage()) {
  return { candidates: [{ content: { parts: [{ text: "ok" }] } }], usageMetadata: u };
}

export interface Seen {
  url: string;
  method: string;
  headers: Headers;
  body: string;
}

/** Stands in for Google: records each request the Worker makes and answers with `handler`. */
export function fakeGoogle(handler: (req: Seen) => Response | Promise<Response>) {
  const seen: Seen[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = new Request(input as RequestInfo, init);
    const s = { url: req.url, method: req.method, headers: req.headers, body: req.body ? await req.text() : "" };
    seen.push(s);
    return handler(s);
  });
  return seen;
}

export function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

export async function usageRow(accountId: string) {
  return env.DB.prepare("SELECT * FROM usage_month WHERE account_id = ?").bind(accountId).first<any>();
}

/** POST /v1beta/models/{role}:generateContent as the app would. */
export function generate(token: string, role: string, headers: Record<string, string> = {}) {
  return call(`/v1beta/models/${role}:generateContent`, {
    method: "POST",
    headers: bearer(token, headers),
    body: JSON.stringify({ contents: [{ role: "user", parts: [{ text: "hi" }] }] }),
  });
}
