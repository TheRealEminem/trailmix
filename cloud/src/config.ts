// Every number and model here is a server setting: change it (or override it with the CLOUD_CONFIG variable) and
// every app follows, with no app update.
import type { Env } from "./env";

export const ROLES = ["transcribe", "transcribe_chunk", "notes", "quick"] as const;
export type Role = (typeof ROLES)[number];
export type Plan = "trial" | "basic" | "pro" | "max";
export type Level = "pro" | "basic";
/** A model per role; null refuses the role at that level. */
export type RoleModels = Record<Role, string | null>;

export interface Price {
  /** Dollars per million tokens. Thinking tokens count as output. */
  input: number;
  audio: number;
  output: number;
}

export interface Config {
  plans: Record<Plan, { proHours: number; models?: Partial<Record<Level, Partial<RoleModels>>> }>;
  models: Record<Level, RoleModels>;
  prices: Record<string, Price>;
  batchDiscount: number;
  /** Gemini bills audio as tokens; meeting seconds are worked out from them. Measured on a real meeting. */
  audioTokensPerSecond: number;
  /** Roles whose audio counts as meeting hours. */
  hourRoles: Role[];
  rateLimitPerMinute: number;
}

export const DEFAULTS: Config = {
  plans: {
    trial: { proHours: 5 },
    basic: { proHours: 0 },
    pro: { proHours: 50 },
    max: { proHours: 150 },
  },
  models: {
    pro: {
      transcribe: "gemini-3.5-flash-lite",
      transcribe_chunk: "gemini-3.5-flash-lite",
      notes: "gemini-3.8-flash",
      quick: "gemini-3.5-flash-lite",
    },
    basic: {
      transcribe: "gemini-3.5-flash-lite",
      transcribe_chunk: null,
      notes: "gemini-3.5-flash-lite",
      quick: "gemini-3.5-flash-lite",
    },
  },
  // Google's rates as of 2026-10-12 (backend/pricing.py). Flash roughly doubles on 2027-01-01.
  prices: {
    "gemini-3.5-flash-lite": { input: 0.3, audio: 0.3, output: 2.5 },
    "gemini-3.1-flash-lite": { input: 0.25, audio: 0.25, output: 1.5 },
    "gemini-3.8-flash": { input: 0.75, audio: 0.75, output: 3.75 },
    "gemini-3.6-flash": { input: 0.75, audio: 0.75, output: 3.75 },
  },
  batchDiscount: 0.5,
  audioTokensPerSecond: 25,
  hourRoles: ["transcribe", "transcribe_chunk"],
  rateLimitPerMinute: 120,
};

function merge<T>(base: T, over: unknown): T {
  if (!over || typeof over !== "object" || Array.isArray(over) || !base || typeof base !== "object" || Array.isArray(base))
    return (over === undefined ? base : over) as T;
  const out: Record<string, unknown> = { ...(base as Record<string, unknown>) };
  for (const [k, v] of Object.entries(over)) out[k] = merge(out[k], v);
  return out as T;
}

let cached: { raw: string | undefined; config: Config } | null = null;

export function config(env: Env): Config {
  if (cached && cached.raw === env.CLOUD_CONFIG) return cached.config;
  cached = { raw: env.CLOUD_CONFIG, config: env.CLOUD_CONFIG ? merge(DEFAULTS, JSON.parse(env.CLOUD_CONFIG)) : DEFAULTS };
  return cached.config;
}

/** The real model for a role on this plan at this level, or null when the level doesn't offer it. */
export function modelFor(cfg: Config, plan: Plan, level: Level, role: Role): string | null {
  const override = cfg.plans[plan]?.models?.[level];
  if (override && role in override) return override[role] ?? null;
  return cfg.models[level][role];
}

export function isRole(s: string): s is Role {
  return (ROLES as readonly string[]).includes(s);
}
