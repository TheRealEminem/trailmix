import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { bearer, BASE, call, link, ORIGIN, signIn, unique } from "./helpers";

describe("linking the app", () => {
  it("hands the app a token once the person approves the code", async () => {
    const start = await (await call("/v1/link", { method: "POST", body: JSON.stringify({ device_name: "Mark's MacBook", device_id: unique() }) })).json<any>();
    expect(start.code).toMatch(/^[A-Z0-9]{4}-[A-Z0-9]{4}$/);
    expect(start.link_url).toBe(`${BASE}/link?code=${start.code}`);
    expect(start.expires_in).toBe(600);
    expect(start.interval).toBe(3);

    const pending = await call("/v1/link/poll", { method: "POST", body: JSON.stringify({ code: start.code }) });
    expect(pending.status).toBe(202);
    expect(await pending.json()).toEqual({ status: "pending" });

    // Not signed in: the link page sends the person to sign in first, and back here after.
    const page = await call(`/link?code=${start.code}`);
    expect(page.status).toBe(303);
    expect(page.headers.get("location")).toBe(`/signin?next=${encodeURIComponent(`/link?code=${start.code}`)}`);

    const cookie = await signIn(`${unique()}@example.com`);
    const confirm = await call(`/link?code=${start.code}`, { headers: { cookie } });
    expect(await confirm.text()).toContain("Mark&#39;s MacBook");
    const approved = await call("/link/approve", { method: "POST", headers: { ...ORIGIN, cookie }, body: new URLSearchParams({ code: start.code }) });
    expect(approved.status).toBe(200);

    const linked = await call("/v1/link/poll", { method: "POST", body: JSON.stringify({ code: start.code }) });
    expect(linked.status).toBe(200);
    const { token } = await linked.json<any>();
    expect(token).toMatch(/^tmc_/);

    // Collected once only.
    const again = await call("/v1/link/poll", { method: "POST", body: JSON.stringify({ code: start.code }) });
    expect(again.status).toBe(410);
    expect(await again.json()).toEqual({ error: "expired" });

    const account = await (await call("/v1/account", { headers: bearer(token) })).json<any>();
    expect(account.plan).toBe("trial");
    expect(account.trial_hours_left).toBe(5);

    // Only a hash of the token is stored.
    const stored = await env.DB.prepare("SELECT token_hash FROM devices WHERE token_hash = ?").bind(token).first();
    expect(stored).toBeNull();
  });

  it("can poll with the secret device code instead of the visible code", async () => {
    const start = await (await call("/v1/link", { method: "POST", body: "{}" })).json<any>();
    const cookie = await signIn(`${unique()}@example.com`);
    await call("/link/approve", { method: "POST", headers: { ...ORIGIN, cookie }, body: new URLSearchParams({ code: start.code }) });
    const linked = await call("/v1/link/poll", { method: "POST", body: JSON.stringify({ device_code: start.device_code }) });
    expect((await linked.json<any>()).token).toMatch(/^tmc_/);
  });

  it("says an old code expired", async () => {
    const start = await (await call("/v1/link", { method: "POST", body: "{}" })).json<any>();
    await env.DB.prepare("UPDATE link_codes SET expires_at = 1 WHERE code = ?").bind(start.code).run();
    const res = await call("/v1/link/poll", { method: "POST", body: JSON.stringify({ code: start.code }) });
    expect(res.status).toBe(410);
    expect(await res.json()).toEqual({ error: "expired" });
  });

  it("refuses an approval posted from another site", async () => {
    const start = await (await call("/v1/link", { method: "POST", body: "{}" })).json<any>();
    const cookie = await signIn(`${unique()}@example.com`);
    const res = await call("/link/approve", {
      method: "POST",
      headers: { origin: "https://evil.test", cookie },
      body: new URLSearchParams({ code: start.code }),
    });
    expect(res.status).toBe(403);
  });

  it("signs out: the token stops working", async () => {
    const { token } = await link();
    expect((await call("/v1/signout", { method: "POST", headers: bearer(token) })).status).toBe(204);
    expect((await call("/v1/account", { headers: bearer(token) })).status).toBe(401);
  });

  it("revokes a Mac from the account page", async () => {
    const { token, cookie, accountId } = await link();
    const device = await env.DB.prepare("SELECT id FROM devices WHERE account_id = ?").bind(accountId).first<{ id: string }>();
    const res = await call("/account/devices/revoke", { method: "POST", headers: { ...ORIGIN, cookie }, body: new URLSearchParams({ id: device!.id }) });
    expect(res.status).toBe(303);
    expect((await call("/v1/account", { headers: bearer(token) })).status).toBe(401);
  });

  it("rejects missing and made-up tokens", async () => {
    expect((await call("/v1/account")).status).toBe(401);
    expect((await call("/v1/account", { headers: bearer("tmc_nope") })).status).toBe(401);
  });

  it("uses a sign-in link only once", async () => {
    const email = `${unique()}@example.com`;
    await signIn(email);
    const used = await env.DB.prepare("SELECT used_at FROM magic_links WHERE email = ?").bind(email).first<{ used_at: number }>();
    expect(used!.used_at).toBeGreaterThan(0);
  });
});

describe("trials", () => {
  it("one per Mac: a second account on the same Mac gets none", async () => {
    const mac = unique("mac");
    const first = await link(undefined, mac);
    const second = await link(undefined, mac);
    expect((await (await call("/v1/account", { headers: bearer(first.token) })).json<any>()).plan).toBe("trial");
    const other = await (await call("/v1/account", { headers: bearer(second.token) })).json<any>();
    expect(other.plan).toBe("none");
    expect(other.status).toBe("lapsed");
    expect(other.trial_hours_left).toBeNull();
  });

  it("one per account: linking another Mac doesn't start a second", async () => {
    const email = `${unique()}@example.com`;
    const first = await link(email);
    await env.DB.prepare("UPDATE accounts SET trial_seconds = 3600 WHERE id = ?").bind(first.accountId).run();
    const second = await link(email);
    const account = await (await call("/v1/account", { headers: bearer(second.token) })).json<any>();
    expect(account.trial_hours_left).toBe(4);
  });

  it("needs a device id", async () => {
    const { token } = await link(undefined, null);
    expect((await (await call("/v1/account", { headers: bearer(token) })).json<any>()).plan).toBe("none");
  });
});
