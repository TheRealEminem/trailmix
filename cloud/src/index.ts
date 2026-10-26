// Trailmix Cloud: accounts, linking the app, billing, and a Gemini-shaped proxy that meters what it passes on.
import {
  emailLink,
  passkeyLoginOptions,
  passkeyLoginVerify,
  passkeyRegisterOptions,
  passkeyRegisterVerify,
  redirect,
  signinPage,
  signout,
  verify,
  verifyPage,
} from "./auth";
import { lemonSqueezyWebhook } from "./billing";
import type { Env } from "./env";
import { accountPage, approveLink, getAccountJson, linkPage, pollLink, revokeDevice, signoutDevice, startLink } from "./link";
import { batch, file, listModels, models, upload } from "./proxy";
import { apiError, json, month } from "./util";

export type { Env };

async function route(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const url = new URL(req.url);
  const p = url.pathname;
  const m = req.method;

  // The Gemini-shaped API the app calls with its device token.
  if (p.startsWith("/v1beta/models/")) return models(env, req, ctx, url);
  if (p === "/v1beta/models" && m === "GET") return listModels(env, req, ctx);
  if (p.startsWith("/v1beta/files/")) return file(env, req, ctx, url);
  if (p.startsWith("/v1beta/batches/")) return batch(env, req, ctx, url);
  if (p === "/upload/v1beta/files" && (m === "POST" || m === "PUT")) return upload(env, req, ctx, url);

  // The app's own account API.
  if (p === "/v1/link" && m === "POST") return startLink(env, req);
  if (p === "/v1/link/poll" && m === "POST") return pollLink(env, req);
  if (p === "/v1/account" && m === "GET") return getAccountJson(env, req, ctx);
  if (p === "/v1/signout" && m === "POST") return signoutDevice(env, req, ctx);

  // Billing.
  if (p === "/webhooks/lemonsqueezy" && m === "POST") return lemonSqueezyWebhook(env, req);

  // Web pages.
  if (p === "/" && m === "GET") return redirect("/account");
  if (p === "/signin" && m === "GET") return signinPage(url);
  if (p === "/auth/email" && m === "POST") return emailLink(env, req);
  if (p === "/auth/verify" && m === "GET") return verifyPage(url);
  if (p === "/auth/verify" && m === "POST") return verify(env, req);
  if (p === "/signout" && m === "POST") return signout(env, req);
  if (p === "/auth/passkey/register/options" && m === "POST") return passkeyRegisterOptions(env, req);
  if (p === "/auth/passkey/register/verify" && m === "POST") return passkeyRegisterVerify(env, req);
  if (p === "/auth/passkey/login/options" && m === "POST") return passkeyLoginOptions(env, req);
  if (p === "/auth/passkey/login/verify" && m === "POST") return passkeyLoginVerify(env, req);
  if (p === "/link" && m === "GET") return linkPage(env, req, url);
  if (p === "/link/approve" && m === "POST") return approveLink(env, req);
  if (p === "/account" && m === "GET") return accountPage(env, req);
  if (p === "/account/devices/revoke" && m === "POST") return revokeDevice(env, req);
  if (p === "/health") return json({ ok: true });

  return apiError(404, "Not found");
}

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    try {
      return await route(req, env, ctx);
    } catch (err) {
      // The method and path only: bodies carry people's meetings and are never logged.
      console.error(`${req.method} ${new URL(req.url).pathname} failed:`, err instanceof Error ? err.message : err);
      return apiError(500, "Trailmix Cloud hit an error; try again");
    }
  },

  /** Daily: clear out expired sign-in links, sessions, link codes, challenges, rate-limit windows and old meeting
   * rows. Usage totals (usage_month) are kept. */
  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    const now = Date.now();
    const lastMonth = month(Date.UTC(new Date(now).getUTCFullYear(), new Date(now).getUTCMonth() - 1, 1));
    await env.DB.batch([
      env.DB.prepare("DELETE FROM magic_links WHERE expires_at < ?").bind(now),
      env.DB.prepare("DELETE FROM sessions WHERE expires_at < ?").bind(now),
      env.DB.prepare("DELETE FROM link_codes WHERE expires_at < ?").bind(now),
      env.DB.prepare("DELETE FROM challenges WHERE expires_at < ?").bind(now),
      env.DB.prepare("DELETE FROM rate_limits WHERE window < ?").bind(Math.floor(now / 1000) - 86400),
      env.DB.prepare("DELETE FROM meeting_streams WHERE month < ?").bind(lastMonth),
      env.DB.prepare("DELETE FROM meetings WHERE month < ?").bind(lastMonth),
      // Google deletes uploaded files after 48 hours; forget them too.
      env.DB.prepare("DELETE FROM files WHERE created_at < ?").bind(now - 3 * 86400_000),
      env.DB.prepare("DELETE FROM uploads WHERE created_at < ?").bind(now - 7 * 86400_000),
    ]);
  },
} satisfies ExportedHandler<Env>;
