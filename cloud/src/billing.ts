// Lemon Squeezy, the merchant of record: its webhook tells us each subscription's plan, status and period end.
// Checkout links carry our account id (checkout[custom][account_id]), which comes back as meta.custom_data.
import { findOrCreateAccount } from "./account";
import type { Env } from "./env";
import { apiError, hmacSha256, json, timingSafeEqual } from "./util";

const PLANS = new Set(["basic", "pro", "max"]);

interface LemonSqueezyEvent {
  meta?: { event_name?: string; custom_data?: { account_id?: string } };
  data?: {
    id?: string;
    type?: string;
    attributes?: {
      status?: string;
      variant_id?: number | string;
      customer_id?: number | string;
      user_email?: string;
      renews_at?: string | null;
      ends_at?: string | null;
      updated_at?: string;
      urls?: { customer_portal?: string };
    };
  };
}

export async function lemonSqueezyWebhook(env: Env, req: Request): Promise<Response> {
  const raw = await req.arrayBuffer();
  const signature = (req.headers.get("x-signature") ?? "").toLowerCase();
  if (!env.LEMONSQUEEZY_SIGNING_SECRET || !timingSafeEqual(signature, await hmacSha256(env.LEMONSQUEEZY_SIGNING_SECRET, raw)))
    return apiError(401, "Bad signature");

  let event: LemonSqueezyEvent;
  try {
    event = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return apiError(400, "Not JSON");
  }
  const name = event.meta?.event_name ?? "";
  const a = event.data?.attributes;
  // Payments, refunds and the like don't change the plan; the subscription_* events that follow them do.
  if (!name.startsWith("subscription_") || event.data?.type !== "subscriptions" || !a) return json({ ignored: name });

  const variants: Record<string, string> = JSON.parse(env.LEMONSQUEEZY_VARIANTS || "{}");
  const plan = variants[String(a.variant_id)];
  if (!plan || !PLANS.has(plan)) return apiError(400, `Unknown variant ${a.variant_id}; add it to LEMONSQUEEZY_VARIANTS`);

  let accountId = event.meta?.custom_data?.account_id;
  if (accountId) {
    const found = await env.DB.prepare("SELECT id FROM accounts WHERE id = ?").bind(accountId).first();
    if (!found) accountId = undefined;
  }
  if (!accountId) {
    // Bought without going through the app's link: the email they paid with becomes (or finds) their account.
    if (!a.user_email) return apiError(400, "No account for this subscription");
    accountId = (await findOrCreateAccount(env, a.user_email)).id;
  }

  const end = a.ends_at ?? a.renews_at;
  await env.DB.prepare(
    `INSERT INTO subscriptions (account_id, provider, provider_id, customer_id, plan, status, period_end, portal_url,
         provider_updated_at, updated_at)
       VALUES (?, 'lemonsqueezy', ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(account_id) DO UPDATE SET provider = excluded.provider, provider_id = excluded.provider_id,
         customer_id = excluded.customer_id, plan = excluded.plan, status = excluded.status, period_end = excluded.period_end,
         portal_url = COALESCE(excluded.portal_url, subscriptions.portal_url),
         provider_updated_at = excluded.provider_updated_at, updated_at = excluded.updated_at
       WHERE excluded.provider_updated_at IS NULL OR subscriptions.provider_updated_at IS NULL
         OR excluded.provider_updated_at >= subscriptions.provider_updated_at`,
  )
    .bind(
      accountId,
      String(event.data?.id ?? ""),
      a.customer_id != null ? String(a.customer_id) : null,
      plan,
      a.status ?? "active",
      end ? Date.parse(end) : null,
      a.urls?.customer_portal ?? null,
      a.updated_at ? Date.parse(a.updated_at) : null,
      Date.now(),
    )
    .run();
  return json({ ok: true, account_id: accountId, plan, status: a.status });
}
