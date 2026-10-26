// Counting what each account used: meeting hours (what people see) and Google's actual cost (what we watch).
import { config, type Config, type Level, type Plan, type Role } from "./config";
import type { Env } from "./env";
import { month } from "./util";

/** Google's usageMetadata, the parts we read. */
export interface UsageMetadata {
  promptTokenCount?: number;
  candidatesTokenCount?: number;
  thoughtsTokenCount?: number;
  promptTokensDetails?: { modality?: string; tokenCount?: number }[];
}

export interface Use {
  accountId: string;
  plan: Plan;
  level: Level;
  role: Role;
  model: string;
  usage: UsageMetadata | null | undefined;
  batch?: boolean;
  /** The app's id for the meeting (X-Trailmix-Meeting) and which track this is (X-Trailmix-Track). */
  meetingId?: string | null;
  track?: string | null;
}

export function audioTokens(usage: UsageMetadata): number {
  return (usage.promptTokensDetails ?? [])
    .filter((d) => d.modality === "AUDIO")
    .reduce((n, d) => n + (d.tokenCount ?? 0), 0);
}

/** What Google charges for one response, in millionths of a dollar. Unknown models are priced like the dearest
 * known one, so a new model never looks free. */
export function costMicros(cfg: Config, model: string, usage: UsageMetadata, batch = false): number {
  const price =
    cfg.prices[model] ??
    Object.values(cfg.prices).reduce((a, b) => (b.output > a.output ? b : a), { input: 0, audio: 0, output: 0 });
  const audio = audioTokens(usage);
  const textIn = Math.max(0, (usage.promptTokenCount ?? 0) - audio);
  const out = (usage.candidatesTokenCount ?? 0) + (usage.thoughtsTokenCount ?? 0);
  const dollars = (audio * price.audio + textIn * price.input + out * price.output) / 1e6;
  return Math.round(dollars * 1e6 * (batch ? cfg.batchDiscount : 1));
}

/** Records one response's usage. Runs as one transaction, so two chunks of a meeting arriving together count once. */
export async function meter(env: Env, use: Use, now = Date.now()): Promise<void> {
  if (!use.usage) return;
  const cfg = config(env);
  const m = month(now);
  const usage = use.usage;
  const cost = costMicros(cfg, use.model, usage, use.batch);
  const inTokens = usage.promptTokenCount ?? 0;
  const outTokens = (usage.candidatesTokenCount ?? 0) + (usage.thoughtsTokenCount ?? 0);
  const seconds = cfg.hourRoles.includes(use.role) ? audioTokens(usage) / cfg.audioTokensPerSecond : 0;

  const db = env.DB;
  const statements = [
    db.prepare("INSERT INTO usage_month (account_id, month) VALUES (?, ?) ON CONFLICT DO NOTHING").bind(use.accountId, m),
    db.prepare(
      `UPDATE usage_month SET cost_micros = cost_micros + ?, requests = requests + 1,
         input_tokens = input_tokens + ?, output_tokens = output_tokens + ? WHERE account_id = ? AND month = ?`,
    ).bind(cost, inTokens, outTokens, use.accountId, m),
  ];

  if (seconds > 0) {
    // Without a meeting id, the request is a meeting of its own.
    const meeting = (use.meetingId || crypto.randomUUID()).slice(0, 100);
    const stream = `${use.role}:${(use.track || "").slice(0, 40)}`;
    const key = [use.accountId, m, meeting];
    // How far the meeting's longest stream now goes past what was already counted.
    const growth = `MAX(0, (SELECT MAX(seconds) FROM meeting_streams WHERE account_id = ?1 AND month = ?2 AND meeting_id = ?3)
                     - COALESCE((SELECT counted_seconds FROM meetings WHERE account_id = ?1 AND month = ?2 AND meeting_id = ?3), 0))`;
    const column = use.level === "pro" ? "pro_seconds" : "basic_seconds";
    statements.push(
      db.prepare(
        `INSERT INTO meeting_streams (account_id, month, meeting_id, stream, seconds) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT DO UPDATE SET seconds = seconds + excluded.seconds`,
      ).bind(...key, stream, seconds),
      db.prepare(`UPDATE usage_month SET ${column} = ${column} + ${growth} WHERE account_id = ?1 AND month = ?2`).bind(...key),
    );
    if (use.plan === "trial")
      statements.push(db.prepare(`UPDATE accounts SET trial_seconds = trial_seconds + ${growth} WHERE id = ?1`).bind(...key));
    statements.push(
      db.prepare(
        `INSERT INTO meetings (account_id, month, meeting_id, counted_seconds)
           VALUES (?1, ?2, ?3, (SELECT MAX(seconds) FROM meeting_streams WHERE account_id = ?1 AND month = ?2 AND meeting_id = ?3))
           ON CONFLICT DO UPDATE SET counted_seconds = MAX(counted_seconds, excluded.counted_seconds)`,
      ).bind(...key),
    );
  }
  await db.batch(statements);
}

/** The last usageMetadata in a streamed reply, whether it came as server-sent events (?alt=sse) or a JSON array. */
export class UsageSniffer {
  private buffer = "";
  private whole = "";
  private sse = false;
  usage: UsageMetadata | null = null;
  private decoder = new TextDecoder();

  push(chunk: Uint8Array) {
    const text = this.decoder.decode(chunk, { stream: true });
    if (!this.sse && this.whole.length < 8_000_000) this.whole += text;
    this.buffer += text;
    let nl: number;
    while ((nl = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, nl).trim();
      this.buffer = this.buffer.slice(nl + 1);
      if (line.startsWith("data:")) {
        this.sse = true;
        this.whole = "";
        this.take(line.slice(5));
      }
    }
  }

  finish(): UsageMetadata | null {
    if (this.buffer.trim().startsWith("data:")) this.take(this.buffer.trim().slice(5));
    else if (!this.sse && this.whole) {
      try {
        const parsed = JSON.parse(this.whole);
        for (const item of Array.isArray(parsed) ? parsed : [parsed]) if (item?.usageMetadata) this.usage = item.usageMetadata;
      } catch {
        // A cut-off reply: nothing to count.
      }
    }
    return this.usage;
  }

  private take(data: string) {
    try {
      const event = JSON.parse(data);
      if (event?.usageMetadata) this.usage = event.usageMetadata; // each event carries the totals so far
    } catch {
      // not JSON; ignore
    }
  }
}
