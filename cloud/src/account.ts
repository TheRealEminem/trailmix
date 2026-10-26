// Who's calling, what plan they're on, and the level they get right now.
import { config, type Level, type Plan } from "./config";
import type { Env } from "./env";
import { apiError, month, nextMonthStart, randomToken, round, sha256 } from "./util";

export interface Account {
  id: string;
  email: string;
  created_at: number;
  trial_started_at: number | null;
  trial_seconds: number;
}

export interface Subscription {
  account_id: string;
  provider: string;
  provider_id: string;
  plan: Exclude<Plan, "trial">;
  status: string;
  period_end: number | null;
  portal_url: string | null;
}

/** Why someone gets 402: only a lapsed subscription, a used-up trial, or never having had either. */
export type Blocked = "lapsed" | "trial_used" | "no_plan";

export interface Standing {
  plan: Plan | null;
  level: Level | null;
  blocked: Blocked | null;
  sub: Subscription | null;
  proSecondsUsed: number;
  proSecondsLimit: number;
  trialSecondsLeft: number;
}

export async function findOrCreateAccount(env: Env, email: string, now = Date.now()): Promise<Account> {
  email = email.trim().toLowerCase();
  await env.DB.prepare("INSERT INTO accounts (id, email, created_at) VALUES (?, ?, ?) ON CONFLICT(email) DO NOTHING")
    .bind(crypto.randomUUID(), email, now)
    .run();
  return (await env.DB.prepare("SELECT * FROM accounts WHERE email = ?").bind(email).first<Account>())!;
}

export async function getAccount(env: Env, id: string): Promise<Account | null> {
  return env.DB.prepare("SELECT * FROM accounts WHERE id = ?").bind(id).first<Account>();
}

/** A subscription that still pays for service. Cancelled ones run to the end of the period they paid for; past_due
 * is the provider's retry window, so it keeps working while the card is retried. */
export function subscriptionLive(sub: Subscription, now: number): boolean {
  if (["active", "on_trial", "past_due"].includes(sub.status)) return true;
  if (sub.status === "cancelled") return (sub.period_end ?? 0) > now;
  return false;
}

export async function standing(env: Env, account: Account, now = Date.now()): Promise<Standing> {
  const cfg = config(env);
  const [sub, usage] = await Promise.all([
    env.DB.prepare("SELECT * FROM subscriptions WHERE account_id = ?").bind(account.id).first<Subscription>(),
    env.DB.prepare("SELECT pro_seconds FROM usage_month WHERE account_id = ? AND month = ?")
      .bind(account.id, month(now))
      .first<{ pro_seconds: number }>(),
  ]);
  const trialLimit = cfg.plans.trial.proHours * 3600;
  // Subscribing ends the trial: a lapsed subscriber doesn't fall back to unused trial hours.
  const trialSecondsLeft = account.trial_started_at != null && !sub ? Math.max(0, trialLimit - account.trial_seconds) : 0;
  const base = { sub, trialSecondsLeft, proSecondsUsed: usage?.pro_seconds ?? 0 };

  let plan: Plan | null = null;
  if (sub && subscriptionLive(sub, now)) plan = sub.plan;
  else if (trialSecondsLeft > 0) plan = "trial";
  if (!plan) {
    const blocked: Blocked = sub ? "lapsed" : account.trial_started_at != null ? "trial_used" : "no_plan";
    return { ...base, plan, level: null, blocked, proSecondsLimit: 0 };
  }
  if (plan === "trial")
    return { ...base, plan, level: "pro", blocked: null, proSecondsUsed: account.trial_seconds, proSecondsLimit: trialLimit };
  const limit = cfg.plans[plan].proHours * 3600;
  return { ...base, plan, level: base.proSecondsUsed < limit ? "pro" : "basic", blocked: null, proSecondsLimit: limit };
}

const BLOCKED_MESSAGE: Record<Blocked, string> = {
  lapsed: "Your Trailmix Cloud subscription has ended. Renew it to keep using Trailmix Cloud.",
  trial_used: "You've used your free Trailmix Cloud hours. Choose a plan to keep going.",
  no_plan: "Choose a Trailmix Cloud plan to start.",
};

export function paymentRequired(env: Env, account: Account, blocked: Blocked): Response {
  return apiError(402, BLOCKED_MESSAGE[blocked], { reason: blocked, upgrade_url: upgradeUrl(env, account, null) });
}

export function checkoutUrls(env: Env, account: Account): Record<string, string> {
  const links: Record<string, string> = JSON.parse(env.CHECKOUT_URLS || "{}");
  const out: Record<string, string> = {};
  for (const [plan, link] of Object.entries(links)) {
    const u = new URL(link);
    u.searchParams.set("checkout[custom][account_id]", account.id);
    out[plan] = u.toString();
  }
  return out;
}

/** The one next step up: Pro for anyone below it, Max for Pro, nothing for Max. Without a checkout link for it, the
 * account page, which lists what there is. */
export function upgradeUrl(env: Env, account: Account, plan: Plan | null): string | null {
  const next = plan === "max" ? null : plan === "pro" ? "max" : "pro";
  if (!next) return null;
  return checkoutUrls(env, account)[next] ?? `${env.BASE_URL}/account`;
}

/** What GET /v1/account returns: everything the app shows about the plan. */
export async function accountSummary(env: Env, account: Account, now = Date.now()) {
  const s = await standing(env, account, now);
  const metered = s.plan === "trial" || s.plan === "pro" || s.plan === "max";
  return {
    email: account.email,
    plan: s.plan ?? "none",
    status: s.blocked ? "lapsed" : "active",
    level: s.level,
    blocked: s.blocked,
    pro_hours_used: round(s.proSecondsUsed / 3600),
    // Basic has no meter: its meetings are unlimited.
    pro_hours_limit: metered ? round(s.proSecondsLimit / 3600) : null,
    trial_hours_left: account.trial_started_at != null ? round(s.trialSecondsLeft / 3600) : null,
    // Trial hours don't come back; Pro and Max hours do, on the 1st.
    resets_at: s.plan === "pro" || s.plan === "max" ? new Date(nextMonthStart(now)).toISOString() : null,
    period_end: s.sub?.period_end ? new Date(s.sub.period_end).toISOString() : null,
    manage_url: s.sub?.portal_url ?? null,
    upgrade_url: upgradeUrl(env, account, s.plan),
    account_url: `${env.BASE_URL}/account`,
    checkout_urls: checkoutUrls(env, account),
  };
}

// ── Devices ─────────────────────────────────────────────────────────────

export interface Device {
  id: string;
  account_id: string;
  name: string | null;
  device_hash: string | null;
  created_at: number;
  last_used_at: number | null;
  revoked_at: number | null;
}

export async function createDevice(env: Env, accountId: string, name: string | null, deviceHash: string | null, now = Date.now()) {
  const token = `tmc_${randomToken()}`;
  const id = crypto.randomUUID();
  await env.DB.prepare("INSERT INTO devices (id, account_id, token_hash, name, device_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)")
    .bind(id, accountId, await sha256(token), name, deviceHash, now)
    .run();
  return { id, token };
}

/** The account and device behind `Authorization: Bearer <device token>`, or a 401. */
export async function authenticate(
  env: Env,
  req: Request,
  ctx: ExecutionContext,
): Promise<{ account: Account; device: Device } | Response> {
  const header = req.headers.get("authorization") ?? "";
  const token = header.match(/^Bearer\s+(tmc_[A-Za-z0-9_-]+)$/)?.[1];
  if (!token) return apiError(401, "Sign in to Trailmix Cloud in the app (Settings → AI providers)");
  const device = await env.DB.prepare("SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL")
    .bind(await sha256(token))
    .first<Device>();
  if (!device) return apiError(401, "This Mac was signed out of Trailmix Cloud. Sign in again in Settings → AI providers");
  const account = await getAccount(env, device.account_id);
  if (!account) return apiError(401, "This Trailmix Cloud account no longer exists");
  const now = Date.now();
  if (!device.last_used_at || device.last_used_at < now - 3600_000)
    ctx.waitUntil(env.DB.prepare("UPDATE devices SET last_used_at = ? WHERE id = ?").bind(now, device.id).run());
  return { account, device };
}

// ── Trials ──────────────────────────────────────────────────────────────

/** Starts the free trial when an account links its first Mac, unless the account or that Mac already had one (or
 * already pays). Any Mac linked to a trial account can't start another trial elsewhere. */
export async function maybeStartTrial(env: Env, account: Account, deviceHash: string | null, now = Date.now()): Promise<boolean> {
  const hasSub = await env.DB.prepare("SELECT 1 FROM subscriptions WHERE account_id = ?").bind(account.id).first();
  if (account.trial_started_at != null) {
    if (deviceHash)
      await env.DB.prepare("INSERT INTO trial_devices (device_hash, account_id, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING")
        .bind(deviceHash, account.id, now)
        .run();
    return false;
  }
  if (hasSub || !deviceHash) return false;
  const claimed = await env.DB.prepare(
    "INSERT INTO trial_devices (device_hash, account_id, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
  )
    .bind(deviceHash, account.id, now)
    .run();
  if (!claimed.meta.changes) return false;
  await env.DB.prepare("UPDATE accounts SET trial_started_at = ? WHERE id = ? AND trial_started_at IS NULL").bind(now, account.id).run();
  return true;
}

// ── Rate limits ─────────────────────────────────────────────────────────

/** A fixed window counter. Returns a 429 once `key` has been seen `limit` times in the current window. */
export async function rateLimit(env: Env, key: string, limit: number, windowSeconds: number, now = Date.now()): Promise<Response | null> {
  const window = Math.floor(now / 1000 / windowSeconds) * windowSeconds; // the window's start, in seconds
  const row = await env.DB.prepare(
    "INSERT INTO rate_limits (key, window, count) VALUES (?, ?, 1) ON CONFLICT(key, window) DO UPDATE SET count = count + 1 RETURNING count",
  )
    .bind(key, window)
    .first<{ count: number }>();
  if ((row?.count ?? 0) <= limit) return null;
  const retry = Math.max(1, Math.ceil(window + windowSeconds - now / 1000));
  return apiError(429, "Too many requests to Trailmix Cloud; slow down a little", {}, { "retry-after": String(retry) });
}
