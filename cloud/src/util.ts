const enc = new TextEncoder();

export function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomToken(bytes = 32): string {
  return b64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export function hex(bytes: ArrayBuffer): string {
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function sha256(s: string): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
}

export async function hmacSha256(secret: string, body: ArrayBuffer | string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, typeof body === "string" ? enc.encode(body) : body));
}

export function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function json(body: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

const STATUS: Record<number, string> = {
  400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 402: "PAYMENT_REQUIRED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
  409: "FAILED_PRECONDITION", 410: "GONE", 429: "RESOURCE_EXHAUSTED", 502: "UNAVAILABLE",
};

/** An error in Google's shape ({error: {code, message, status}}), so the app reads it like Gemini's own. */
export function apiError(code: number, message: string, extra: Record<string, unknown> = {}, headers: HeadersInit = {}): Response {
  return json({ error: { code, message, status: STATUS[code] ?? "UNKNOWN" }, ...extra }, code, headers);
}

export function month(now: number): string {
  return new Date(now).toISOString().slice(0, 7);
}

/** Milliseconds at the start of next month (UTC), when Pro-level hours come back. */
export function nextMonthStart(now: number): number {
  const d = new Date(now);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

export function round(n: number, places = 2): number {
  const f = 10 ** places;
  return Math.round(n * f) / f;
}
