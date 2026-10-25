// Linking the app, like signing in a TV: the app asks for a code, the person approves it in their browser, and the
// app (polling with a secret only it knows) collects a device token it keeps until it signs out or is revoked.
import {
  accountSummary,
  authenticate,
  checkoutUrls,
  createDevice,
  maybeStartTrial,
  rateLimit,
  standing,
  type Device,
} from "./account";
import { redirect, sameOrigin, sessionAccount } from "./auth";
import { config } from "./config";
import type { Env } from "./env";
import { e, page } from "./pages";
import { apiError, json, randomToken, sha256 } from "./util";

const LINK_MINUTES = 10;
const POLL_SECONDS = 3;
// No 0/O, 1/I/L: easy to read aloud and to check against the app.
const ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";

function userCode(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  const chars = [...bytes].map((b) => ALPHABET[b % ALPHABET.length]).join("");
  return `${chars.slice(0, 4)}-${chars.slice(4)}`;
}

interface LinkRow {
  code: string;
  device_name: string | null;
  device_hash: string | null;
  expires_at: number;
  account_id: string | null;
  device_token: string | null;
  delivered_at: number | null;
}

type LinkBody = { device_name?: string; name?: string; device_id?: string };

/** POST /v1/link {device_name, device_id?} → {code, link_url, expires_in, interval}. `device_id` is a stable id for
 * this Mac; only its hash is kept, to allow one free trial per Mac (without it, linking works but starts no trial). */
export async function startLink(env: Env, req: Request): Promise<Response> {
  const ip = req.headers.get("cf-connecting-ip") ?? "unknown";
  const limited = await rateLimit(env, `link:${ip}`, 30, 600);
  if (limited) return limited;
  const body = await req.json<LinkBody>().catch(() => ({}) as LinkBody);
  const name = body.device_name ?? body.name;
  const deviceHash = body.device_id ? await sha256(`device:${String(body.device_id)}`) : null;
  const deviceCode = randomToken();
  const now = Date.now();
  for (let attempt = 0; ; attempt++) {
    const code = userCode();
    const inserted = await env.DB.prepare(
      `INSERT INTO link_codes (code, poll_hash, device_name, device_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(code) DO NOTHING`,
    )
      .bind(code, await sha256(deviceCode), name ? String(name).slice(0, 80) : null, deviceHash, now, now + LINK_MINUTES * 60_000)
      .run();
    if (inserted.meta.changes) {
      return json({
        code,
        link_url: `${env.BASE_URL}/link?code=${code}`,
        expires_in: LINK_MINUTES * 60,
        interval: POLL_SECONDS,
        // A secret only this app knows. Polling with it instead of the code means someone who only saw the code
        // (it's on screen and in the browser's address bar) can't collect the token first.
        device_code: deviceCode,
      });
    }
    if (attempt > 5) return apiError(502, "Couldn't make a link code; try again");
  }
}

/** POST /v1/link/poll {code} (or {device_code}): 202 {status: "pending"} while waiting, 200 {token} once and only once,
 * 410 {error: "expired"} when the code expired or was already collected. */
export async function pollLink(env: Env, req: Request): Promise<Response> {
  const body = await req.json<{ code?: string; device_code?: string }>().catch(() => ({}) as { code?: string; device_code?: string });
  const where = body.device_code ? "poll_hash = ?" : "code = ?";
  const key = body.device_code ? await sha256(body.device_code) : String(body.code ?? "").trim().toUpperCase();
  if (!key) return apiError(400, "code is missing");
  const ip = req.headers.get("cf-connecting-ip") ?? "unknown";
  const limited = await rateLimit(env, `poll:${ip}`, 120, 60);
  if (limited) return limited;
  const row = await env.DB.prepare(`SELECT * FROM link_codes WHERE ${where}`).bind(key).first<LinkRow>();
  if (!row) return apiError(404, "No such link request");
  const expired = json({ error: "expired" }, 410);
  if (row.delivered_at) return expired;
  if (row.account_id && row.device_token) {
    // Hand the token over once and forget it. Only the poll that clears it gets it.
    const taken = await env.DB.prepare(
      `UPDATE link_codes SET device_token = NULL, delivered_at = ? WHERE ${where} AND delivered_at IS NULL AND device_token = ?`,
    )
      .bind(Date.now(), key, row.device_token)
      .run();
    if (!taken.meta.changes) return expired;
    return json({ status: "linked", token: row.device_token });
  }
  if (row.expires_at < Date.now()) return expired;
  return json({ status: "pending" }, 202);
}

async function pendingLink(env: Env, code: string): Promise<LinkRow | null> {
  return env.DB.prepare("SELECT * FROM link_codes WHERE code = ? AND expires_at > ? AND account_id IS NULL")
    .bind(code.toUpperCase(), Date.now())
    .first<LinkRow>();
}

/** GET /link?code=…: sign in if needed, then confirm linking this Mac. */
export async function linkPage(env: Env, req: Request, url: URL): Promise<Response> {
  const code = (url.searchParams.get("code") ?? "").trim().toUpperCase();
  const account = await sessionAccount(env, req);
  if (!account) return redirect(`/signin?next=${encodeURIComponent(`/link?code=${code}`)}`);
  const row = code ? await pendingLink(env, code) : null;
  if (!row)
    return page("That code has expired", `<p>Codes last ${LINK_MINUTES} minutes. Start again from Trailmix on your Mac.</p>`, 404);
  return page(
    "Link Trailmix",
    `<div class="card">
      <p>Check that this code matches the one in Trailmix:</p>
      <p class="code">${e(row.code)}</p>
      <p>Link <strong>${e(row.device_name ?? "this Mac")}</strong> to <strong>${e(account.email)}</strong>?</p>
      <form method="post" action="/link/approve"><input type="hidden" name="code" value="${e(row.code)}">
        <button type="submit">Link this Mac</button></form>
    </div>
    <p class="muted">Not you? <a href="/account">Switch account</a></p>`,
  );
}

export async function approveLink(env: Env, req: Request): Promise<Response> {
  if (!sameOrigin(env, req)) return apiError(403, "Cross-site request refused");
  const account = await sessionAccount(env, req);
  if (!account) return redirect("/signin");
  const code = String((await req.formData()).get("code") ?? "");
  const row = await pendingLink(env, code);
  if (!row) return page("That code has expired", `<p>Start again from Trailmix on your Mac.</p>`, 404);

  const { token } = await createDevice(env, account.id, row.device_name, row.device_hash);
  const claimed = await env.DB.prepare("UPDATE link_codes SET account_id = ?, device_token = ? WHERE code = ? AND account_id IS NULL")
    .bind(account.id, token, row.code)
    .run();
  if (!claimed.meta.changes) return page("That code was already used", `<p>Start again from Trailmix on your Mac.</p>`, 409);
  const trial = await maybeStartTrial(env, account, row.device_hash);
  const hasPasskey = await env.DB.prepare("SELECT 1 FROM passkeys WHERE account_id = ?").bind(account.id).first();
  return page(
    "Trailmix is linked",
    `<div class="card"><p>You can go back to Trailmix now.</p>
      ${trial ? `<p>Your ${config(env).plans.trial.proHours} free hours at the Pro level have started.</p>` : ""}</div>
    ${hasPasskey ? "" : `<div class="card"><p>Sign in faster next time with a passkey.</p>
      <button class="secondary" onclick="passkeyRegister()">Add a passkey</button><p class="muted" id="passkey-msg"></p></div>`}`,
  );
}

// ── The app's account endpoints ─────────────────────────────────────────

export async function getAccountJson(env: Env, req: Request, ctx: ExecutionContext): Promise<Response> {
  const auth = await authenticate(env, req, ctx);
  if (auth instanceof Response) return auth;
  return json(await accountSummary(env, auth.account));
}

/** POST /v1/signout: the app forgets its token, and so do we. */
export async function signoutDevice(env: Env, req: Request, ctx: ExecutionContext): Promise<Response> {
  const auth = await authenticate(env, req, ctx);
  if (auth instanceof Response) return auth;
  await env.DB.prepare("UPDATE devices SET revoked_at = ? WHERE id = ?").bind(Date.now(), auth.device.id).run();
  return new Response(null, { status: 204 });
}

// ── The account page ────────────────────────────────────────────────────

function when(ms: number | null): string {
  return ms ? new Date(ms).toISOString().slice(0, 10) : "never";
}

export async function accountPage(env: Env, req: Request): Promise<Response> {
  const account = await sessionAccount(env, req);
  if (!account) return redirect("/signin?next=/account");
  const [s, summary, devices, passkeys] = await Promise.all([
    standing(env, account),
    accountSummary(env, account),
    env.DB.prepare("SELECT * FROM devices WHERE account_id = ? AND revoked_at IS NULL ORDER BY created_at")
      .bind(account.id)
      .all<Device>(),
    env.DB.prepare("SELECT COUNT(*) AS n FROM passkeys WHERE account_id = ?").bind(account.id).first<{ n: number }>(),
  ]);
  const planName = s.plan ? { trial: "Free trial", basic: "Basic", pro: "Pro", max: "Max" }[s.plan] : "No plan";
  const usage =
    s.plan === "trial"
      ? `${summary.trial_hours_left} of ${summary.pro_hours_limit} free hours left`
      : s.plan === "pro" || s.plan === "max"
        ? `${summary.pro_hours_used} of ${summary.pro_hours_limit} Pro-level hours used this month` +
          (s.level === "basic" ? " (at the Basic level until the 1st)" : "")
        : s.plan === "basic"
          ? "Unlimited meetings"
          : "Choose a plan to use Trailmix Cloud";
  const checkout = checkoutUrls(env, account);
  const choose = Object.entries(checkout)
    .map(([plan, link]) => `<a class="button" href="${e(link)}">${e(plan[0].toUpperCase() + plan.slice(1))}</a>`)
    .join(" ");
  return page(
    "Your account",
    `<div class="card"><p><strong>${e(account.email)}</strong></p>
      <p>${e(planName)}: ${e(usage)}</p>
      ${summary.manage_url ? `<a class="button" href="${e(summary.manage_url)}">Manage subscription</a>` : choose}
    </div>
    <div class="card"><p><strong>Linked Macs</strong></p>
      ${
        devices.results.length
          ? `<table>${devices.results
              .map(
                (d) => `<tr><td>${e(d.name ?? "A Mac")}<br><span class="muted">linked ${when(d.created_at)}, last used ${when(d.last_used_at)}</span></td>
                <td style="text-align:right"><form class="inline" method="post" action="/account/devices/revoke">
                <input type="hidden" name="id" value="${e(d.id)}"><button class="secondary">Sign out</button></form></td></tr>`,
              )
              .join("")}</table>`
          : `<p class="muted">None yet. In Trailmix, choose Trailmix Cloud in Settings → AI providers.</p>`
      }
    </div>
    <div class="card"><p><strong>Passkeys</strong></p>
      <p class="muted">${passkeys?.n ? `${passkeys.n} added.` : "None yet."}</p>
      <button class="secondary" onclick="passkeyRegister()">Add a passkey</button><p class="muted" id="passkey-msg"></p>
    </div>
    <form method="post" action="/signout"><button class="secondary">Sign out of this browser</button></form>`,
  );
}

export async function revokeDevice(env: Env, req: Request): Promise<Response> {
  if (!sameOrigin(env, req)) return apiError(403, "Cross-site request refused");
  const account = await sessionAccount(env, req);
  if (!account) return redirect("/signin");
  const id = String((await req.formData()).get("id") ?? "");
  await env.DB.prepare("UPDATE devices SET revoked_at = ? WHERE id = ? AND account_id = ?").bind(Date.now(), id, account.id).run();
  return redirect("/account");
}
