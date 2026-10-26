import { useEffect, useState } from "react";
import { api } from "../api";
import type { CloudAccount, Usage } from "../api";
import { periodDate, planLine } from "./Cloud";
import { setSpendWarn } from "../spend";
import { ChevronDownIcon } from "./icons";
import { Collapse, ControlRow, Segmented, Select } from "./ui";

const PERIODS = [
  { id: "7", label: "7 days" },
  { id: "30", label: "30 days" },
  { id: "90", label: "90 days" },
];

const WARN_OPTIONS = [
  { value: "25", label: "$0.25" },
  { value: "50", label: "$0.50" },
  { value: "100", label: "$1" },
  { value: "500", label: "$5" },
  { value: "0", label: "Never ask" },
];

/** What the work is called here. Anything else is shown as it was recorded. */
const KIND_NAMES: Record<string, string> = {
  transcript: "Transcripts",
  "transcript (batch)": "Transcripts (batch)",
  "live captions": "Live captions",
  notes: "Notes and titles",
  "notes (batch)": "Notes (batch)",
  "live notes": "Notes during meetings",
  "catch up": "Catch-up questions",
  questions: "Questions",
  attachments: "Attachments",
  "tags & sorting": "Tags and sorting",
  other: "Everything else",
};

const kindName = (k: string) => KIND_NAMES[k] ?? k.charAt(0).toUpperCase() + k.slice(1);

export const dollars = (c: number) => (c < 0.005 ? "under $0.01" : `$${c.toFixed(2)}`);

const compact = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n));

const duration = (sec: number) => {
  const min = Math.round(sec / 60);
  return min >= 60 ? `${Math.floor(min / 60)} h ${min % 60} min` : `${min} min`;
};

interface Line {
  kind: string;
  cost: number;
  unpriced: number;
  seconds: number;
  tokens: number;
  n: number;
  models: string[];
}

/** The usage rows added up by what the work was for, biggest first. */
function byKind(rows: Usage["rows"]): Line[] {
  const lines = new Map<string, Line>();
  for (const r of rows) {
    const l = lines.get(r.kind) ?? { kind: r.kind, cost: 0, unpriced: 0, seconds: 0, tokens: 0, n: 0, models: [] };
    l.cost += r.cost ?? 0;
    l.unpriced += r.unpriced;
    l.seconds += r.seconds;
    l.tokens += (r.tokens_in ?? 0) + (r.tokens_out ?? 0);
    l.n += r.n;
    const model = r.model ?? r.engine;
    if (!l.models.includes(model)) l.models.push(model);
    lines.set(r.kind, l);
  }
  return [...lines.values()].sort((a, b) => b.cost - a.cost || b.tokens + b.seconds - (a.tokens + a.seconds));
}

const amount = (l: Line) =>
  [l.seconds ? `${duration(l.seconds)} of audio` : "", l.tokens ? `${compact(l.tokens)} tokens` : ""].filter(Boolean).join(" · ") ||
  `${l.n} time${l.n === 1 ? "" : "s"}`;

/**
 * Settings → Usage: how much paid AI Trailmix has used, like the usage page of an AI app. The total is always on show;
 * the breakdown is one click further. Prices are estimates; tokens and minutes are what the provider reported or what
 * was sent.
 */
export default function UsagePanel({
  warnCents,
  onWarnCents,
  cloud,
}: {
  warnCents: number;
  onWarnCents: (cents: number) => void;
  /** Trailmix Cloud's account when signed in: its work is part of the plan, so it's shown in hours, not dollars */
  cloud?: CloudAccount | null;
}) {
  const [days, setDays] = useState("30");
  const [usage, setUsage] = useState<Usage | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let alive = true;
    api
      .usage(Number(days))
      .then((u) => alive && setUsage(u))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [days]);

  const lines = usage ? byKind(usage.rows) : [];
  const top = Math.max(0, ...lines.map((l) => l.cost));
  const total = usage ? (usage.rows.length === 0 ? "Nothing yet" : usage.cost > 0 ? `About ${dollars(usage.cost)}` : "No cost counted") : "…";
  const period = PERIODS.find((p) => p.id === days)?.label.toLowerCase();

  const limit = cloud?.pro_hours_limit;
  const used = cloud?.pro_hours_used ?? 0;

  return (
    <>
      {cloud?.plan && (
        <div className="py-3.5">
          <div className="text-label font-medium text-ink">Trailmix Cloud</div>
          <div className="mt-0.5 text-ui text-ink">{planLine(cloud)}</div>
          {limit ? (
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-subtle" role="presentation">
              <div className="h-full rounded-full bg-forest" style={{ width: `${Math.min(100, (used / limit) * 100)}%` }} />
            </div>
          ) : null}
          <p className="mt-1.5 text-hint leading-[1.45] text-ink-soft">
            Part of your plan: nothing more to pay.
            {limit ? ` Live hours come back on ${cloud.period_end ? periodDate(cloud.period_end) : "the 1st"}.` : ""}
          </p>
        </div>
      )}
      <div className="py-3.5">
        <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-2">
          <div>
            <div className="text-label font-medium text-ink">Paid AI used, last {period}</div>
            <div className="mt-0.5 text-heading font-semibold tabular-nums tracking-[-0.01em] text-ink">{total}</div>
          </div>
          <Segmented value={days} options={PERIODS} onChange={setDays} label="Period" size="sm" />
        </div>
        <p className="mt-1 text-hint leading-[1.45] text-ink-soft">
          An estimate from published prices (as of {usage?.as_of ?? "…"}), not your bill.
          {usage?.unpriced ? " Some work is counted in tokens only: its price isn't known." : ""} AI that runs on this Mac is
          free and not counted.
        </p>
        {lines.length > 0 && (
          <button
            type="button"
            className="mt-2 inline-flex items-center gap-1 text-hint font-medium text-forest-deep hover:underline"
            onClick={() => setOpen(!open)}
            aria-expanded={open}
          >
            {open ? "Hide breakdown" : "Show breakdown"}
            <ChevronDownIcon size={14} className={`transition-transform ${open ? "rotate-180" : ""}`} />
          </button>
        )}
        <Collapse open={open}>
          <ul className="mt-3 space-y-3">
            {lines.map((l) => (
              <li key={l.kind}>
                <div className="flex items-baseline justify-between gap-3 text-label">
                  <span className="font-medium text-ink">{kindName(l.kind)}</span>
                  <span className="tabular-nums text-ink">
                    {l.cost > 0 ? dollars(l.cost) : l.unpriced ? "price unknown" : "free"}
                    {l.cost > 0 && l.unpriced > 0 ? " + unpriced" : ""}
                  </span>
                </div>
                <div
                  className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-subtle"
                  role="presentation"
                >
                  <div className="h-full rounded-full bg-forest" style={{ width: `${top ? Math.max(2, (l.cost / top) * 100) : 0}%` }} />
                </div>
                <div className="mt-1 text-hint text-ink-soft">
                  {amount(l)} · {l.models.join(", ")}
                </div>
              </li>
            ))}
          </ul>
        </Collapse>
      </div>
      <ControlRow
        label="Ask before anything over"
        hint="Small jobs just run. A job that would cost this much or more (redoing lots of meetings, a long recording) asks first, with the price."
        wrap
      >
        <Select
          label="Ask before anything over"
          value={String(warnCents)}
          onChange={(v) => {
            setSpendWarn(Number(v));
            onWarnCents(Number(v));
          }}
          className="w-full sm:w-40"
          options={WARN_OPTIONS.some((o) => o.value === String(warnCents)) ? WARN_OPTIONS : [...WARN_OPTIONS, { value: String(warnCents), label: dollars(warnCents / 100) }]}
        />
      </ControlRow>
    </>
  );
}
