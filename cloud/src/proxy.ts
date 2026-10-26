// The Gemini-shaped endpoints. The app sends a role where Gemini expects a model; we pick the model for the account's
// plan and level, call Google with our key, and meter what came back. Request and response bodies are never logged.
import { authenticate, paymentRequired, rateLimit, standing, type Account, type Standing } from "./account";
import { config, isRole, modelFor, ROLES, type Role } from "./config";
import type { Env } from "./env";
import { meter, UsageSniffer } from "./metering";
import { apiError, json } from "./util";

const GOOGLE = "https://generativelanguage.googleapis.com";

function upstream(env: Env): string {
  return (env.GEMINI_BASE || GOOGLE).replace(/\/$/, "");
}

function googleHeaders(env: Env, req: Request, pass: string[] = []): Headers {
  const h = new Headers({ "x-goog-api-key": env.GEMINI_API_KEY });
  for (const name of ["content-type", ...pass]) {
    const v = req.headers.get(name);
    if (v != null) h.set(name, v);
  }
  return h;
}

/** Query parameters to pass on (alt=sse, pageSize…), never a key someone tried to slip in. */
function query(url: URL): string {
  const q = new URLSearchParams(url.search);
  q.delete("key");
  const s = q.toString();
  return s ? `?${s}` : "";
}

/** Google's reply as ours, except that a refusal of *our* key isn't the app's problem to explain. */
function relay(res: Response, extra: Record<string, string> = {}): Response {
  if (res.status === 401 || res.status === 403)
    return apiError(502, "Trailmix Cloud couldn't reach Google just now; try again in a minute", {}, extra);
  const headers = new Headers();
  for (const name of ["content-type", "retry-after"]) {
    const v = res.headers.get(name);
    if (v) headers.set(name, v);
  }
  for (const [k, v] of Object.entries(extra)) headers.set(k, v);
  return new Response(res.body, { status: res.status, headers });
}

interface Caller {
  account: Account;
  standing: Standing;
}

/** Signed in, under the rate limit, and (unless `block` is false) paying or on a trial with hours left. Reading or
 * deleting what an account already has (files, finished batch jobs) works even after its plan lapses. */
async function caller(env: Env, req: Request, ctx: ExecutionContext, block = true): Promise<Caller | Response> {
  const auth = await authenticate(env, req, ctx);
  if (auth instanceof Response) return auth;
  const s = await standing(env, auth.account);
  if (s.blocked && block) return paymentRequired(env, auth.account, s.blocked);
  const limited = await rateLimit(env, `acct:${auth.account.id}`, config(env).rateLimitPerMinute, 60);
  if (limited) return limited;
  return { account: auth.account, standing: s };
}

/** On every proxied reply, so the app notices a level change without polling /v1/account. */
function levelHeaders(c: Caller, model?: string): Record<string, string> {
  if (!c.standing.level) return {};
  return { "x-trailmix-level": c.standing.level, "x-trailmix-plan": c.standing.plan!, ...(model ? { "x-trailmix-model": model } : {}) };
}

/** The model for a role, or the reply that refuses it. */
function pick(env: Env, c: Caller, roleName: string): { role: Role; model: string } | Response {
  if (!isRole(roleName))
    return apiError(404, `Trailmix Cloud has no role "${roleName}"; use one of ${ROLES.join(", ")}`);
  const model = modelFor(config(env), c.standing.plan!, c.standing.level!, roleName);
  if (!model)
    return apiError(
      409,
      "This month's Pro-level hours are used up, so live captions are paused until the 1st. Meetings still get a transcript right after.",
      { level: c.standing.level },
      levelHeaders(c),
    );
  return { role: roleName, model };
}

// ── generateContent / streamGenerateContent / batchGenerateContent ─────

export async function models(env: Env, req: Request, ctx: ExecutionContext, url: URL): Promise<Response> {
  const c = await caller(env, req, ctx);
  if (c instanceof Response) return c;
  const roleName = decodeURIComponent(url.pathname.slice("/v1beta/models/".length));
  const [name, method] = roleName.split(":");
  const picked = pick(env, c, name);
  if (picked instanceof Response) return picked;
  const { role, model } = picked;

  if (req.method !== "POST") return apiError(404, "Not found");
  if (method === "batchGenerateContent") return batchCreate(env, req, c, role, model, url);
  if (method !== "generateContent" && method !== "streamGenerateContent") return apiError(404, `Trailmix Cloud doesn't offer :${method}`);

  const res = await fetch(`${upstream(env)}/v1beta/models/${model}:${method}${query(url)}`, {
    method: "POST",
    headers: googleHeaders(env, req),
    body: req.body,
  });
  const use = {
    accountId: c.account.id,
    plan: c.standing.plan!,
    level: c.standing.level!,
    role,
    model,
    meetingId: req.headers.get("x-trailmix-meeting"),
    track: req.headers.get("x-trailmix-track"),
  };
  if (!res.ok || !res.body) return relay(res, levelHeaders(c, model));

  if (method === "generateContent") {
    const text = await res.text();
    try {
      ctx.waitUntil(meter(env, { ...use, usage: JSON.parse(text).usageMetadata }));
    } catch {
      // Not JSON; nothing to count.
    }
    return relay(new Response(text, res), levelHeaders(c, model));
  }

  // Streaming: pass every chunk straight through and count the totals in the last event once the reply is done.
  const sniffer = new UsageSniffer();
  let done!: () => void;
  const finished = new Promise<void>((resolve) => (done = resolve));
  const counted = new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      sniffer.push(chunk);
      controller.enqueue(chunk);
    },
    flush() {
      done();
    },
  });
  ctx.waitUntil(finished.then(() => meter(env, { ...use, usage: sniffer.finish() })));
  return relay(new Response(res.body.pipeThrough(counted), res), levelHeaders(c, model));
}

/** GET /v1beta/models: the roles, listed the way Google lists models. */
export async function listModels(env: Env, req: Request, ctx: ExecutionContext): Promise<Response> {
  const c = await caller(env, req, ctx);
  if (c instanceof Response) return c;
  const cfg = config(env);
  return json(
    {
      models: ROLES.map((role) => ({
        name: `models/${role}`,
        displayName: role,
        available: modelFor(cfg, c.standing.plan!, c.standing.level!, role) != null,
        supportedGenerationMethods: ["generateContent", "streamGenerateContent", "batchGenerateContent"],
      })),
    },
    200,
    levelHeaders(c),
  );
}

// ── Batches ────────────────────────────────────────────────────────────

async function batchCreate(env: Env, req: Request, c: Caller, role: Role, model: string, url: URL): Promise<Response> {
  const res = await fetch(`${upstream(env)}/v1beta/models/${model}:batchGenerateContent${query(url)}`, {
    method: "POST",
    headers: googleHeaders(env, req),
    body: req.body,
  });
  if (!res.ok) return relay(res, levelHeaders(c, model));
  const text = await res.text();
  let name: string | undefined;
  try {
    name = JSON.parse(text).name;
  } catch {
    // handled below
  }
  if (!name?.startsWith("batches/")) return apiError(502, "Google didn't give the batch job a name");
  await env.DB.prepare("INSERT INTO batches (name, account_id, role, plan, level, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
    .bind(name, c.account.id, role, c.standing.plan, c.standing.level, model, Date.now())
    .run();
  return relay(new Response(text, res), levelHeaders(c, model));
}

interface BatchRow {
  name: string;
  account_id: string;
  role: Role;
  plan: Caller["standing"]["plan"] & string;
  level: "pro" | "basic";
  model: string;
  metered_at: number | null;
}

/** The answers of a finished job, wherever this version of the API put them (see backend/gemini_batch.py). */
function batchUsages(job: any): any[] {
  const holders = [job?.response, job?.metadata?.output, job?.dest].filter(Boolean);
  for (const h of holders) {
    const items = h.inlinedResponses?.inlinedResponses ?? h.inlinedResponses;
    if (Array.isArray(items)) return items.map((i: any) => i?.response?.usageMetadata).filter(Boolean);
  }
  return [];
}

/** GET /v1beta/batches/{id} and POST /v1beta/batches/{id}:cancel. A finished job is metered once, at the batch price. */
export async function batch(env: Env, req: Request, ctx: ExecutionContext, url: URL): Promise<Response> {
  const auth = await caller(env, req, ctx, false);
  if (auth instanceof Response) return auth;
  const path = decodeURIComponent(url.pathname.slice("/v1beta/".length));
  const [name, method] = path.split(":");
  const row = await env.DB.prepare("SELECT * FROM batches WHERE name = ? AND account_id = ?").bind(name, auth.account.id).first<BatchRow>();
  if (!row) return apiError(404, "No such batch job");

  if (req.method === "POST" && method === "cancel") {
    const res = await fetch(`${upstream(env)}/v1beta/${name}:cancel`, { method: "POST", headers: googleHeaders(env, req), body: "{}" });
    return relay(res, levelHeaders(auth));
  }
  if (req.method !== "GET" || method) return apiError(404, "Not found");

  const res = await fetch(`${upstream(env)}/v1beta/${name}`, { headers: googleHeaders(env, req) });
  if (!res.ok) return relay(res, levelHeaders(auth));
  const text = await res.text();
  let job: any = null;
  try {
    job = JSON.parse(text);
  } catch {
    // passed through as is
  }
  const usages = batchUsages(job);
  if (usages.length && row.metered_at == null) {
    ctx.waitUntil(
      (async () => {
        const claimed = await env.DB.prepare("UPDATE batches SET metered_at = ? WHERE name = ? AND metered_at IS NULL")
          .bind(Date.now(), name)
          .run();
        if (!claimed.meta.changes) return;
        for (const usage of usages)
          await meter(env, { accountId: row.account_id, plan: row.plan, level: row.level, role: row.role, model: row.model, usage, batch: true });
      })(),
    );
  }
  return relay(new Response(text, res), levelHeaders(auth));
}

// ── Files ──────────────────────────────────────────────────────────────

/** POST /upload/v1beta/files: starts a resumable upload (no upload_id) or sends its bytes (with one). The bytes stream
 * straight through to Google; the upload address Google hands back is rewritten to point here. */
export async function upload(env: Env, req: Request, ctx: ExecutionContext, url: URL): Promise<Response> {
  const c = await caller(env, req, ctx);
  if (c instanceof Response) return c;
  const uploadId = url.searchParams.get("upload_id");
  const pass = [...req.headers.keys()].filter((k) => k.startsWith("x-goog-upload-"));

  if (!uploadId) {
    const res = await fetch(`${upstream(env)}/upload/v1beta/files${query(url)}`, {
      method: req.method,
      headers: googleHeaders(env, req, pass),
      body: req.body,
    });
    const target = res.headers.get("x-goog-upload-url");
    if (!res.ok || !target) return relay(res, levelHeaders(c));
    const real = new URL(target);
    const id = real.searchParams.get("upload_id");
    if (!id) return apiError(502, "Google didn't give an upload address");
    await env.DB.prepare("INSERT INTO uploads (upload_id, account_id, upstream_url, created_at) VALUES (?, ?, ?, ?)")
      .bind(id, c.account.id, target, Date.now())
      .run();
    const ours = new URL(`${env.BASE_URL.replace(/\/$/, "")}/upload/v1beta/files`);
    real.searchParams.forEach((v, k) => k !== "key" && ours.searchParams.set(k, v));
    const out = relay(res, levelHeaders(c));
    out.headers.set("x-goog-upload-url", ours.toString());
    for (const name of ["x-goog-upload-status", "x-goog-upload-chunk-granularity"]) {
      const v = res.headers.get(name);
      if (v) out.headers.set(name, v);
    }
    return out;
  }

  const row = await env.DB.prepare("SELECT upstream_url FROM uploads WHERE upload_id = ? AND account_id = ?")
    .bind(uploadId, c.account.id)
    .first<{ upstream_url: string }>();
  if (!row) return apiError(404, "No such upload");
  const headers = googleHeaders(env, req, pass);
  const length = req.headers.get("content-length");
  let body: ReadableStream | null = req.body;
  if (body && length) {
    // Keep the length, so Google sees an ordinary upload rather than a chunked one.
    const fixed = new FixedLengthStream(Number(length));
    body.pipeTo(fixed.writable).catch(() => {});
    body = fixed.readable;
    headers.set("content-length", length);
  }
  const res = await fetch(row.upstream_url, { method: req.method, headers, body });
  const text = await res.text();
  try {
    const name: string | undefined = JSON.parse(text)?.file?.name;
    if (name?.startsWith("files/"))
      await env.DB.batch([
        env.DB.prepare("INSERT INTO files (name, account_id, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING").bind(
          name,
          c.account.id,
          Date.now(),
        ),
        env.DB.prepare("DELETE FROM uploads WHERE upload_id = ?").bind(uploadId),
      ]);
  } catch {
    // An unfinished chunk's reply has no file in it.
  }
  const out = relay(new Response(text, res), levelHeaders(c));
  const status = res.headers.get("x-goog-upload-status");
  if (status) out.headers.set("x-goog-upload-status", status);
  const size = res.headers.get("x-goog-upload-size-received");
  if (size) out.headers.set("x-goog-upload-size-received", size);
  return out;
}

/** GET and DELETE /v1beta/files/{id}, for files this account uploaded. */
export async function file(env: Env, req: Request, ctx: ExecutionContext, url: URL): Promise<Response> {
  const auth = await caller(env, req, ctx, false);
  if (auth instanceof Response) return auth;
  const name = decodeURIComponent(url.pathname.slice("/v1beta/".length));
  const row = await env.DB.prepare("SELECT 1 FROM files WHERE name = ? AND account_id = ?").bind(name, auth.account.id).first();
  if (!row) return apiError(404, "No such file");
  if (req.method !== "GET" && req.method !== "DELETE") return apiError(404, "Not found");
  const res = await fetch(`${upstream(env)}/v1beta/${name}`, { method: req.method, headers: googleHeaders(env, req) });
  if (req.method === "DELETE" && (res.ok || res.status === 404))
    await env.DB.prepare("DELETE FROM files WHERE name = ?").bind(name).run();
  return relay(res, levelHeaders(auth));
}
