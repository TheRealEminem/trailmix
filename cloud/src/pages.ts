// The few web pages people see: signing in, approving a Mac, and their account.
import { escapeHtml as e } from "./util";

export { e };

export function page(title: string, body: string, status = 200, headers: HeadersInit = {}): Response {
  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${e(title)} · Trailmix Cloud</title>
<style>
  :root { color-scheme: light dark; --fg: #1d1d1f; --muted: #6e6e73; --bg: #fbfbfd; --card: #fff; --line: #d2d2d7; --accent: #2f6f4f; }
  @media (prefers-color-scheme: dark) { :root { --fg: #f5f5f7; --muted: #a1a1a6; --bg: #111; --card: #1c1c1e; --line: #3a3a3c; --accent: #6fbf8f; } }
  body { margin: 0; font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--fg); background: var(--bg); }
  main { max-width: 440px; margin: 0 auto; padding: 48px 16px; }
  h1 { font-size: 24px; margin: 0 0 16px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 20px; margin: 16px 0; }
  .muted { color: var(--muted); font-size: 14px; }
  input[type=email] { width: 100%; box-sizing: border-box; font: inherit; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: transparent; color: inherit; }
  button, .button { display: inline-block; font: inherit; padding: 10px 16px; border-radius: 8px; border: 0; background: var(--accent); color: #fff; cursor: pointer; text-decoration: none; margin-top: 12px; }
  button.secondary { background: transparent; color: var(--accent); border: 1px solid var(--line); }
  table { width: 100%; border-collapse: collapse; font-size: 14px; } td { padding: 6px 0; border-top: 1px solid var(--line); }
  form.inline { display: inline; } form.inline button { margin: 0; padding: 4px 10px; font-size: 13px; }
  .code { font: 600 28px ui-monospace, Menlo, monospace; letter-spacing: 2px; }
</style></head>
<body><main><h1>${e(title)}</h1>${body}</main>${PASSKEY_JS}</body></html>`;
  return new Response(html, { status, headers: { "content-type": "text/html; charset=utf-8", ...headers } });
}

// Passkeys in the browser, without a library: options come from the server as JSON, and the credential goes back
// as JSON, with the binary fields as base64url.
const PASSKEY_JS = `<script>
const b64 = { dec: s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(s.length / 4) * 4, '=')), c => c.charCodeAt(0)),
  enc: b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '') };
async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.error && j.error.message) || 'Something went wrong');
  return j;
}
function say(msg) { const el = document.getElementById('passkey-msg'); if (el) el.textContent = msg; }
async function passkeyRegister() {
  try {
    const o = await post('/auth/passkey/register/options');
    o.challenge = b64.dec(o.challenge); o.user.id = b64.dec(o.user.id);
    o.excludeCredentials = (o.excludeCredentials || []).map(c => ({ ...c, id: b64.dec(c.id) }));
    const c = await navigator.credentials.create({ publicKey: o });
    await post('/auth/passkey/register/verify', { id: c.id, rawId: b64.enc(c.rawId), type: c.type,
      clientExtensionResults: c.getClientExtensionResults(), authenticatorAttachment: c.authenticatorAttachment,
      response: { clientDataJSON: b64.enc(c.response.clientDataJSON), attestationObject: b64.enc(c.response.attestationObject),
        transports: c.response.getTransports ? c.response.getTransports() : [] } });
    say('Passkey added. Next time, sign in with it.');
    setTimeout(() => location.reload(), 800);
  } catch (err) { say(err.message); }
}
async function passkeyLogin(next) {
  try {
    const o = await post('/auth/passkey/login/options');
    o.challenge = b64.dec(o.challenge);
    const c = await navigator.credentials.get({ publicKey: o });
    await post('/auth/passkey/login/verify', { id: c.id, rawId: b64.enc(c.rawId), type: c.type,
      clientExtensionResults: c.getClientExtensionResults(), authenticatorAttachment: c.authenticatorAttachment,
      response: { clientDataJSON: b64.enc(c.response.clientDataJSON), authenticatorData: b64.enc(c.response.authenticatorData),
        signature: b64.enc(c.response.signature), userHandle: c.response.userHandle ? b64.enc(c.response.userHandle) : undefined } });
    location.href = next || '/account';
  } catch (err) { say(err.message); }
}
</script>`;
