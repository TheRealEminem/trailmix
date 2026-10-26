# Trailmix Cloud

The server behind the app's "Trailmix Cloud" mode. It's a Cloudflare Worker with a D1 database. It does four things:
- signs people in, with an emailed link or a passkey;
- links the app, the way you sign in a TV;
- keeps each account's plan in step with Lemon Squeezy;
- proxies Gemini on our key, metering what each account uses.

Models, prices, hours and the rate limit are all settings in [src/config.ts](src/config.ts). Override them without a
code change by setting the `CLOUD_CONFIG` variable to a JSON object. It's merged over the defaults, so it holds only
the keys you change.

## Run it locally

```bash
cd cloud
npm install
cp .dev.vars.example .dev.vars        # then put a Gemini key in it
npm run db:migrate:local
npm run dev                           # http://localhost:8787
```

With `DEV_MODE=1` (the default in wrangler.jsonc), sign-in links are printed in the `wrangler dev` output instead of
being emailed.

To try linking:
1. `curl -XPOST localhost:8787/v1/link -d '{"device_name":"My Mac","device_id":"test"}'`
2. Open the `link_url` it returns and sign in.
3. Poll `/v1/link/poll` with `{"code": …}` to get the token.

Point the app at the Worker with `TRAILMIX_CLOUD_URL=http://localhost:8787`.

## Test

```bash
npm test          # vitest, running inside workerd through Miniflare; Google is faked
npm run typecheck
```

## The API

The app sends `Authorization: Bearer tmc_…` (its device token) on everything except the two link calls.

| | |
|---|---|
| `POST /v1/link` `{device_name, device_id}` | `{code, link_url, expires_in, interval, device_code}`. `device_id` is a stable per-Mac id. Only its hash is kept, for one trial per Mac; without it, no trial starts. |
| `POST /v1/link/poll` `{code}` or `{device_code}` | 202 `{status:"pending"}`, then 200 `{status:"linked", token}` once, or 410 `{error:"expired"}` |
| `GET /v1/account` | `{email, plan, status, level, pro_hours_used, pro_hours_limit, trial_hours_left, period_end, manage_url, upgrade_url, …}` |
| `POST /v1/signout` | 204. Revokes this token. |
| `POST /v1beta/models/{role}:generateContent`, `:streamGenerateContent`, `:batchGenerateContent` | `role` is `transcribe`, `transcribe_chunk`, `notes` or `quick`. The server picks the model for the plan and level. |
| `POST /upload/v1beta/files` | Resumable upload. The returned `x-goog-upload-url` points back here, and the bytes stream through to Google. |
| `GET`/`DELETE /v1beta/files/{id}`, `GET /v1beta/batches/{id}`, `POST /v1beta/batches/{id}:cancel` | Only for this account's own files and jobs. |

- **Levels.** Every proxied reply carries `x-trailmix-level: pro|basic`. Past a plan's Pro-level hours (trial 5,
  Pro 50, Max 150), Pro and Max drop to the Basic level until the 1st:
  - notes switch to flash-lite;
  - `transcribe_chunk` gets 409 `{"level":"basic"}`.
- **402.** Only a lapsed subscription, a used-up trial, or no plan at all gets 402. The body has a sentence to show
  the person and an `upgrade_url`.
- **Meeting hours** are worked out from the audio tokens Google reports (25 per second). Send
  `X-Trailmix-Meeting: <id>` and `X-Trailmix-Track: mic|call` on transcription calls. The meeting then counts as
  long as its longest stream, rather than as the sum of its tracks and passes.
- **Cost** comes from each reply's `usageMetadata` and goes into `usage_month.cost_micros`, in millionths of a dollar.

Request and response bodies are never logged.

## Deploy (when you're ready; nothing here is deployed yet)

1. `npx wrangler d1 create trailmix`, then put the id in `wrangler.jsonc`.
2. `npx wrangler d1 migrations apply trailmix --remote`
3. Add the secrets: `npx wrangler secret put GEMINI_API_KEY` (use a paid-tier key, since its data isn't used for
   training), then do the same for `LEMONSQUEEZY_SIGNING_SECRET` and `RESEND_API_KEY`.
4. In `wrangler.jsonc`, set:
   - `BASE_URL` to the real URL;
   - `DEV_MODE` to `"0"`;
   - `EMAIL_FROM` to an address on a domain verified with Resend;
   - `LEMONSQUEEZY_VARIANTS` (`{"<variant id>": "basic"|"pro"|"max"}`);
   - `CHECKOUT_URLS` (`{"basic": "<checkout link>", …}`).
5. In Lemon Squeezy, point a webhook at `<BASE_URL>/webhooks/lemonsqueezy` for the `subscription_*` events, with the
   same signing secret.
6. `npm run deploy`

Only Lemon Squeezy is wired up. Paddle would be a second handler beside [src/billing.ts](src/billing.ts) that writes
the same `subscriptions` row.

## Layout

| File | What it does |
|---|---|
| `src/index.ts` | Routes, plus the daily cleanup cron |
| `src/config.ts` | Plans, role → model per level, prices |
| `src/account.ts` | Plan, level, 402s, device tokens, trials, rate limits |
| `src/proxy.ts` | The Gemini-shaped endpoints |
| `src/metering.ts` | Cost and meeting hours |
| `src/link.ts` | Linking the app, `/v1/account`, the account page |
| `src/auth.ts` | Email links, passkeys, sessions |
| `src/billing.ts` | The Lemon Squeezy webhook |
| `migrations/` | The D1 schema |
