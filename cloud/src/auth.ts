// Signing in on the web: an emailed link, or a passkey once one is added. Sessions are a cookie whose hash is kept.
import {
  generateAuthenticationOptions,
  generateRegistrationOptions,
  verifyAuthenticationResponse,
  verifyRegistrationResponse,
} from "@simplewebauthn/server";
import { findOrCreateAccount, getAccount, rateLimit, type Account } from "./account";
import type { Env } from "./env";
import { e, page } from "./pages";
import { apiError, json, randomToken, sha256 } from "./util";

const SESSION_COOKIE = "tm_session";
const SESSION_DAYS = 30;
const MAGIC_LINK_MINUTES = 15;
const CHALLENGE_MINUTES = 5;

// ── Sessions ────────────────────────────────────────────────────────────

function cookies(req: Request): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (req.headers.get("cookie") ?? "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

function cookie(env: Env, name: string, value: string, maxAge: number): string {
  const secure = env.BASE_URL.startsWith("https:") ? "; Secure" : "";
  return `${name}=${encodeURIComponent(value)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure}`;
}

export async function sessionAccount(env: Env, req: Request): Promise<Account | null> {
  const token = cookies(req)[SESSION_COOKIE];
  if (!token) return null;
  const row = await env.DB.prepare("SELECT account_id FROM sessions WHERE token_hash = ? AND expires_at > ?")
    .bind(await sha256(token), Date.now())
    .first<{ account_id: string }>();
  return row ? getAccount(env, row.account_id) : null;
}

async function startSession(env: Env, accountId: string): Promise<string> {
  const token = randomToken();
  const now = Date.now();
  await env.DB.prepare("INSERT INTO sessions (token_hash, account_id, created_at, expires_at) VALUES (?, ?, ?, ?)")
    .bind(await sha256(token), accountId, now, now + SESSION_DAYS * 86400_000)
    .run();
  return cookie(env, SESSION_COOKIE, token, SESSION_DAYS * 86400);
}

/** Cookie-authenticated POSTs must come from our own pages. */
export function sameOrigin(env: Env, req: Request): boolean {
  return req.headers.get("origin") === new URL(env.BASE_URL).origin;
}

/** Only paths on this site, so a sign-in link can't send someone elsewhere. */
export function safeNext(next: string | null | undefined): string {
  return next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") ? next : "/account";
}

export function redirect(location: string, headers: HeadersInit = {}): Response {
  return new Response(null, { status: 303, headers: { location, ...headers } });
}

export function signinPage(url: URL): Response {
  const next = safeNext(url.searchParams.get("next"));
  return page(
    "Sign in to Trailmix Cloud",
    `<div class="card">
      <form method="post" action="/auth/email">
        <label for="email">Email</label>
        <input type="email" id="email" name="email" required autocomplete="username webauthn" autofocus>
        <input type="hidden" name="next" value="${e(next)}">
        <button type="submit">Email me a sign-in link</button>
      </form>
    </div>
    <div class="card">
      <p class="muted">Added a passkey before?</p>
      <button class="secondary" onclick="passkeyLogin(${e(JSON.stringify(next))})">Sign in with a passkey</button>
      <p class="muted" id="passkey-msg"></p>
    </div>`,
  );
}

// ── Email links ─────────────────────────────────────────────────────────

async function sendEmail(env: Env, to: string, link: string): Promise<void> {
  if (env.DEV_MODE === "1" && !env.RESEND_API_KEY) {
    console.log(`[dev] sign-in link for ${to}: ${link}`);
    return;
  }
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { authorization: `Bearer ${env.RESEND_API_KEY}`, "content-type": "application/json" },
    body: JSON.stringify({
      from: env.EMAIL_FROM,
      to,
      subject: "Sign in to Trailmix Cloud",
      text: `Open this link to sign in to Trailmix Cloud:\n\n${link}\n\nIt works once, for ${MAGIC_LINK_MINUTES} minutes. If you didn't ask for it, ignore this email.`,
    }),
  });
  if (!res.ok) throw new Error(`email provider returned ${res.status}`);
}

export async function emailLink(env: Env, req: Request): Promise<Response> {
  if (!sameOrigin(env, req)) return apiError(403, "Cross-site request refused");
  const form = await req.formData();
  const email = String(form.get("email") ?? "").trim().toLowerCase();
  const next = safeNext(String(form.get("next") ?? ""));
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email) || email.length > 254)
    return page("Sign in to Trailmix Cloud", `<p>That doesn't look like an email address. <a href="/signin">Try again</a>.</p>`, 400);
  const ip = req.headers.get("cf-connecting-ip") ?? "unknown";
  const limited = (await rateLimit(env, `email:${email}`, 5, 600)) ?? (await rateLimit(env, `ip:${ip}`, 20, 600));
  if (limited) return page("Slow down a little", `<p>Too many sign-in emails. Try again in a few minutes.</p>`, 429);

  const token = randomToken();
  await env.DB.prepare("INSERT INTO magic_links (token_hash, email, next, expires_at) VALUES (?, ?, ?, ?)")
    .bind(await sha256(token), email, next, Date.now() + MAGIC_LINK_MINUTES * 60_000)
    .run();
  await sendEmail(env, email, `${env.BASE_URL}/auth/verify?token=${token}`);
  return page(
    "Check your email",
    `<div class="card"><p>We sent a sign-in link to <strong>${e(email)}</strong>. Open it on this device.</p>
     <p class="muted">It works once, for ${MAGIC_LINK_MINUTES} minutes.</p></div>`,
  );
}

/** GET shows a button and POST signs in, so mail scanners that open links don't use them up. */
export function verifyPage(url: URL): Response {
  const token = url.searchParams.get("token") ?? "";
  return page(
    "Sign in to Trailmix Cloud",
    `<form method="post" action="/auth/verify"><input type="hidden" name="token" value="${e(token)}">
     <button type="submit">Continue</button></form>`,
  );
}

export async function verify(env: Env, req: Request): Promise<Response> {
  if (!sameOrigin(env, req)) return apiError(403, "Cross-site request refused");
  const token = String((await req.formData()).get("token") ?? "");
  const row = await env.DB.prepare(
    "UPDATE magic_links SET used_at = ? WHERE token_hash = ? AND used_at IS NULL AND expires_at > ? RETURNING email, next",
  )
    .bind(Date.now(), await sha256(token), Date.now())
    .first<{ email: string; next: string | null }>();
  if (!row)
    return page("That link has expired", `<p>Sign-in links work once, for ${MAGIC_LINK_MINUTES} minutes. <a href="/signin">Get a new one</a>.</p>`, 400);
  const account = await findOrCreateAccount(env, row.email);
  return redirect(safeNext(row.next), { "set-cookie": await startSession(env, account.id) });
}

export async function signout(env: Env, req: Request): Promise<Response> {
  if (!sameOrigin(env, req)) return apiError(403, "Cross-site request refused");
  const token = cookies(req)[SESSION_COOKIE];
  if (token) await env.DB.prepare("DELETE FROM sessions WHERE token_hash = ?").bind(await sha256(token)).run();
  return redirect("/signin", { "set-cookie": cookie(env, SESSION_COOKIE, "", 0) });
}

// ── Passkeys ────────────────────────────────────────────────────────────

function rp(env: Env) {
  const u = new URL(env.BASE_URL);
  return { rpID: u.hostname, origin: u.origin };
}

async function saveChallenge(env: Env, challenge: string, accountId: string | null): Promise<string> {
  const id = randomToken(16);
  await env.DB.prepare("INSERT INTO challenges (id, challenge, account_id, expires_at) VALUES (?, ?, ?, ?)")
    .bind(id, challenge, accountId, Date.now() + CHALLENGE_MINUTES * 60_000)
    .run();
  return cookie(env, "tm_challenge", id, CHALLENGE_MINUTES * 60);
}

async function takeChallenge(env: Env, req: Request): Promise<{ challenge: string; account_id: string | null } | null> {
  const id = cookies(req)["tm_challenge"];
  if (!id) return null;
  return env.DB.prepare("DELETE FROM challenges WHERE id = ? AND expires_at > ? RETURNING challenge, account_id")
    .bind(id, Date.now())
    .first();
}

export async function passkeyRegisterOptions(env: Env, req: Request): Promise<Response> {
  if (!sameOrigin(env, req)) return apiError(403, "Cross-site request refused");
  const account = await sessionAccount(env, req);
  if (!account) return apiError(401, "Sign in first");
  const existing = await env.DB.prepare("SELECT id, transports FROM passkeys WHERE account_id = ?")
    .bind(account.id)
    .all<{ id: string; transports: string | null }>();
  const options = await generateRegistrationOptions({
    rpName: "Trailmix Cloud",
    rpID: rp(env).rpID,
    userName: account.email,
    userID: new Uint8Array(new TextEncoder().encode(account.id)),
    attestationType: "none",
    excludeCredentials: existing.results.map((p) => ({ id: p.id, transports: p.transports ? JSON.parse(p.transports) : undefined })),
    authenticatorSelection: { residentKey: "required", userVerification: "preferred" },
  });
  return json(options, 200, { "set-cookie": await saveChallenge(env, options.challenge, account.id) });
}

export async function passkeyRegisterVerify(env: Env, req: Request): Promise<Response> {
  if (!sameOrigin(env, req)) return apiError(403, "Cross-site request refused");
  const account = await sessionAccount(env, req);
  const saved = await takeChallenge(env, req);
  if (!account || !saved || saved.account_id !== account.id) return apiError(400, "That took too long; try again");
  const { rpID, origin } = rp(env);
  let result;
  try {
    result = await verifyRegistrationResponse({ response: await req.json(), expectedChallenge: saved.challenge, expectedOrigin: origin, expectedRPID: rpID });
  } catch {
    return apiError(400, "The passkey couldn't be checked; try again");
  }
  if (!result.verified) return apiError(400, "The passkey couldn't be checked; try again");
  const { credential } = result.registrationInfo;
  await env.DB.prepare("INSERT INTO passkeys (id, account_id, public_key, counter, transports, created_at) VALUES (?, ?, ?, ?, ?, ?)")
    .bind(credential.id, account.id, credential.publicKey, credential.counter, JSON.stringify(credential.transports ?? []), Date.now())
    .run();
  return json({ ok: true });
}

export async function passkeyLoginOptions(env: Env, req: Request): Promise<Response> {
  if (!sameOrigin(env, req)) return apiError(403, "Cross-site request refused");
  // Discoverable credentials: the browser offers whichever passkeys it has for this site.
  const options = await generateAuthenticationOptions({ rpID: rp(env).rpID, userVerification: "preferred" });
  return json(options, 200, { "set-cookie": await saveChallenge(env, options.challenge, null) });
}

export async function passkeyLoginVerify(env: Env, req: Request): Promise<Response> {
  if (!sameOrigin(env, req)) return apiError(403, "Cross-site request refused");
  const saved = await takeChallenge(env, req);
  if (!saved) return apiError(400, "That took too long; try again");
  const response = await req.json<any>();
  const key = await env.DB.prepare("SELECT * FROM passkeys WHERE id = ?")
    .bind(String(response?.id ?? ""))
    .first<{ id: string; account_id: string; public_key: ArrayBuffer | number[]; counter: number; transports: string | null }>();
  if (!key) return apiError(400, "This passkey isn't registered with Trailmix Cloud");
  const { rpID, origin } = rp(env);
  let result;
  try {
    result = await verifyAuthenticationResponse({
      response,
      expectedChallenge: saved.challenge,
      expectedOrigin: origin,
      expectedRPID: rpID,
      credential: {
        id: key.id,
        publicKey: new Uint8Array(key.public_key as ArrayBuffer),
        counter: key.counter,
        transports: key.transports ? JSON.parse(key.transports) : undefined,
      },
    });
  } catch {
    return apiError(400, "The passkey couldn't be checked; try again");
  }
  if (!result.verified) return apiError(400, "The passkey couldn't be checked; try again");
  await env.DB.prepare("UPDATE passkeys SET counter = ?, last_used_at = ? WHERE id = ?")
    .bind(result.authenticationInfo.newCounter, Date.now(), key.id)
    .run();
  return json({ ok: true }, 200, { "set-cookie": await startSession(env, key.account_id) });
}
