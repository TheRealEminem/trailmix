import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { bearer, call, fakeGoogle, generate, jsonResponse, link, reply, subscribe, unique, webhook } from "./helpers";

async function account(token: string) {
  return (await call("/v1/account", { headers: bearer(token) })).json<any>();
}

describe("Lemon Squeezy webhook → plan", () => {
  it("rejects a bad signature", async () => {
    const { accountId } = await link();
    const res = await webhook({ variant_id: 102 }, { accountId, secret: "wrong" });
    expect(res.status).toBe(401);
    const sub = await env.DB.prepare("SELECT * FROM subscriptions WHERE account_id = ?").bind(accountId).first();
    expect(sub).toBeNull();
  });

  it("sets the plan, status, period end and portal link", async () => {
    const { token, accountId } = await link();
    const res = await webhook({ variant_id: 102, renews_at: "2099-02-01T00:00:00Z" }, { accountId, event: "subscription_created" });
    expect(res.status).toBe(200);
    const a = await account(token);
    expect(a).toMatchObject({
      plan: "pro",
      status: "active",
      level: "pro",
      pro_hours_used: 0,
      pro_hours_limit: 50,
      period_end: "2099-02-01T00:00:00.000Z",
      manage_url: "https://shop.test/portal",
    });
    expect(a.upgrade_url).toContain("https://shop.test/buy/max");
    expect(a.upgrade_url).toContain(encodeURIComponent(accountId));
  });

  it("Basic has no meter", async () => {
    const { token, accountId } = await link();
    await subscribe(accountId, "basic");
    expect(await account(token)).toMatchObject({ plan: "basic", level: "basic", pro_hours_limit: null });
  });

  it("finds the account by email when the checkout didn't carry one", async () => {
    const email = `${unique()}@example.com`;
    const { token } = await link(email);
    const res = await webhook({ variant_id: 103, user_email: email.toUpperCase() });
    expect(res.status).toBe(200);
    expect((await account(token)).plan).toBe("max");
  });

  it("refuses a variant it doesn't know", async () => {
    const { accountId } = await link();
    expect((await webhook({ variant_id: 999 }, { accountId })).status).toBe(400);
  });

  it("keeps a cancelled plan until the period it paid for ends", async () => {
    const { token, accountId } = await link();
    await webhook({ variant_id: 102, status: "cancelled", ends_at: "2099-01-01T00:00:00Z" }, { accountId });
    expect(await account(token)).toMatchObject({ plan: "pro", status: "active" });
    await webhook({ variant_id: 102, status: "cancelled", ends_at: "2000-01-01T00:00:00Z" }, { accountId });
    expect(await account(token)).toMatchObject({ plan: "none", status: "lapsed", blocked: "lapsed" });
  });

  it("ignores an older event that arrives late", async () => {
    const { token, accountId } = await link();
    await webhook({ variant_id: 103, updated_at: "2026-10-02T00:00:00Z" }, { accountId });
    await webhook({ variant_id: 102, updated_at: "2026-10-01T00:00:00Z" }, { accountId });
    expect((await account(token)).plan).toBe("max");
  });
});

describe("402", () => {
  it("a lapsed subscription gets 402 with a sentence and an upgrade link", async () => {
    const { token, accountId } = await link();
    await subscribe(accountId, "pro", "expired");
    const seen = fakeGoogle(() => jsonResponse(reply()));
    const res = await generate(token, "notes");
    expect(res.status).toBe(402);
    const body = await res.json<any>();
    expect(body.error.message).toMatch(/subscription has ended/);
    expect(body.reason).toBe("lapsed");
    expect(body.upgrade_url).toContain("https://shop.test/buy/pro");
    expect(seen).toHaveLength(0);
  });

  it("a used-up trial gets 402", async () => {
    const { token, accountId } = await link();
    await env.DB.prepare("UPDATE accounts SET trial_seconds = ? WHERE id = ?").bind(5 * 3600, accountId).run();
    const res = await generate(token, "notes");
    expect(res.status).toBe(402);
    expect((await res.json<any>()).reason).toBe("trial_used");
  });

  it("no plan at all gets 402", async () => {
    const { token } = await link(undefined, null);
    const res = await generate(token, "quick");
    expect(res.status).toBe(402);
    expect((await res.json<any>()).reason).toBe("no_plan");
  });

  it("a paused or unpaid subscription gets 402, past_due doesn't yet", async () => {
    const { token, accountId } = await link();
    fakeGoogle(() => jsonResponse(reply()));
    await subscribe(accountId, "basic", "past_due");
    expect((await generate(token, "quick")).status).toBe(200);
    await subscribe(accountId, "basic", "unpaid");
    expect((await generate(token, "quick")).status).toBe(402);
  });
});
