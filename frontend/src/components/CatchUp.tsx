import { useEffect, useMemo, useRef, useState } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import type { CatchUpEntry, CatchUpKind, CatchUpPlan } from "../api";
import { formatDuration } from "../format";
import { useBigSpend } from "../spend";
import { AlertIcon, ArrowUpIcon, CompassIcon, SparkleIcon } from "./icons";
import { Spinner } from "./ui";

const QUICK: { kind: Exclude<CatchUpKind, "question">; label: string; tip: string }[] = [
  { kind: "missed", label: "What did I miss?", tip: "The gist of the last five minutes" },
  { kind: "explain", label: "Explain that", tip: "The last thing said, in plainer words" },
  { kind: "decided", label: "What's been decided?", tip: "Decisions so far, and what's still open" },
];
const PLAN_REFRESH_MS = 30_000; // the price grows with the transcript
const SHOWN = 3;
const PLUGINS = [remarkGfm];

/** About what one question costs: "free" on this Mac, "0.4¢", "$0.012"; null where the price isn't known. */
function money(c: number | null | undefined, local: boolean) {
  if (local) return "free";
  if (c === null || c === undefined) return null;
  return c < 0.01 ? `${(c * 100).toFixed(c < 0.001 ? 2 : 1)}¢` : `$${c.toFixed(3)}`;
}

/** "on this Mac", or what a question costs when that's worth saying (else nothing: Settings → Usage keeps the tally). */
function priceNote(plan: CatchUpPlan, big: boolean) {
  if (plan.local) return "on this Mac";
  const each = big ? money(plan.cost, false) : null;
  return each ? `about ${each} a question` : null;
}

function priceTip(plan: CatchUpPlan) {
  if (plan.local || !plan.tokens || plan.cost === null) return undefined;
  const n = (x: number) => x.toLocaleString();
  return `About ${n(plan.tokens.in)} tokens sent and ${n(plan.tokens.out)} written (thinking included), ${
    plan.measured ? "counted the way Google counted your last answer" : "a cautious guess until Google has counted an answer"
  }, at Google's prices as of ${plan.as_of}. It grows as the meeting goes on.`;
}

/** [12:34] and [1:02:03] in an answer become links that show that moment in the live transcript. */
const linkTimes = (text: string) =>
  text.replace(/\[((?:\d+:)?\d{1,2}:\d\d)\](?!\()/g, (_, t: string) => {
    const secs = t.split(":").reduce((acc, n) => acc * 60 + Number(n), 0);
    return `[${t}](#t-${secs})`;
  });

/** "Catch me up": quick questions about the meeting so far, while it's still recording (backend/catch_up.py). The
 * state lives here; the recording bar puts the questions (CatchUpAsk) and the answers (CatchUpAnswers) where they fit.
 * `onAsked` runs when a question goes out, so the answers can be brought into view. */
export function useCatchUp(meetingId: number, onAsked?: () => void) {
  const [plan, setPlan] = useState<CatchUpPlan | null>(null);
  const [entries, setEntries] = useState<CatchUpEntry[]>([]);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = pending !== null;

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .catchUpPlan(meetingId)
        .then((p) => {
          if (!alive) return;
          setPlan(p);
          setEntries((xs) => (xs.length > p.history.length ? xs : p.history));
        })
        .catch(() => alive && setPlan(null));
    void load();
    const t = setInterval(load, PLAN_REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [meetingId]);

  const ask = async (kind: CatchUpKind, label: string, text = "") => {
    if (busy) return;
    setPending(label);
    setError(null);
    onAsked?.();
    if (kind === "question") setQuestion("");
    try {
      const e = await api.catchUp(meetingId, kind, text);
      setEntries((xs) => [...xs, e]);
    } catch (e) {
      if (kind === "question") setQuestion(text);
      setError((e as Error).message);
    } finally {
      setPending(null);
    }
  };

  return { plan, entries, question, setQuestion, pending, error, busy, ready: !!plan?.ready, ask };
}

export type CatchUp = ReturnType<typeof useCatchUp>;

/** Who answers, e.g. "Gemini · gemini-3.6-flash"; with the price of a question once that's a lot. */
export function CatchUpPrice({ c }: { c: CatchUp }) {
  const big = useBigSpend();
  if (!c.plan?.ready) return null;
  const note = priceNote(c.plan, big(c.plan.cost));
  return (
    <span className="text-meta text-ink-soft" data-tip={priceTip(c.plan)}>
      {c.plan.label}
      {c.plan.model ? ` · ${c.plan.model}` : ""}
      {note ? ` · ${note}` : ""}
    </span>
  );
}

/** The quick questions and a box for your own, in one row that fits the recording bar. */
export function CatchUpAsk({ c }: { c: CatchUp }) {
  const { plan, busy, ready } = c;
  const big = useBigSpend();
  if (plan && !ready) return null;
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        const q = c.question.trim();
        if (q) void c.ask("question", q, q);
      }}
      className="flex flex-wrap items-center gap-2"
      data-testid="catch-up"
    >
      <div className="field flex min-w-0 flex-1 basis-64 items-center gap-2 py-0.5 pl-3.5 pr-1 focus-within:border-[color:color-mix(in_srgb,var(--forest)_55%,transparent)] focus-within:shadow-focus">
        <CompassIcon size={15} className="shrink-0 text-forest" />
        <input
          value={c.question}
          onChange={(e) => c.setQuestion(e.target.value)}
          placeholder="Ask anything about the meeting so far…"
          className="h-8 min-w-0 flex-1 bg-transparent text-label outline-none"
          disabled={busy || !ready}
          aria-label="Question about the meeting so far"
        />
        {plan && !plan.local && big(plan.costs.question) && money(plan.costs.question, false) && (
          <span className="hidden shrink-0 text-meta tabular-nums text-ink-soft sm:inline" data-tip="About what this question costs">
            ~{money(plan.costs.question, false)}
          </span>
        )}
        <button
          type="submit"
          disabled={busy || !ready || !c.question.trim()}
          className="btn btn-primary h-7 w-7 shrink-0 rounded-sm p-0"
          aria-label="Ask"
          data-tip="Ask (Enter)"
        >
          <ArrowUpIcon size={15} strokeWidth={2.2} />
        </button>
      </div>
      <div className="flex max-w-full gap-1.5 overflow-x-auto">
        {QUICK.map((q) => {
          const off = plan?.off[q.kind];
          const cost = plan && !plan.local && big(plan.costs[q.kind]) ? money(plan.costs[q.kind], false) : null;
          return (
            <button
              key={q.kind}
              type="button"
              onClick={() => void c.ask(q.kind, q.label)}
              disabled={busy || !ready || !!off}
              className="btn btn-sm btn-soft shrink-0 rounded-full px-3 font-normal"
              data-tip={off ?? (cost ? `${q.tip} · about ${cost}` : q.tip)}
            >
              {q.label}
            </button>
          );
        })}
      </div>
    </form>
  );
}

/** Answers so far, newest first, with what's being asked now on top. Timestamps show that moment in the transcript. */
export function CatchUpAnswers({ c, onSeek }: { c: CatchUp; onSeek: (seconds: number) => void }) {
  const [showAll, setShowAll] = useState(false);
  const { plan, pending, error } = c;
  // The recording screen re-renders every second (the timer). Stable link components keep the timestamp buttons
  // from being swapped out under the pointer, which would lose the click.
  const seekRef = useRef(onSeek);
  seekRef.current = onSeek;
  const components = useMemo<Components>(
    () => ({
      a: ({ href, children }) =>
        href?.startsWith("#t-") ? (
          <button
            onClick={() => seekRef.current(Number(href.slice(3)))}
            className="chip mx-0.5 bg-sun-soft align-middle font-mono text-sun-deep no-underline transition-colors hover:bg-sun/30"
            data-tip="Show in the live transcript"
          >
            {children}
          </button>
        ) : (
          <a href={href} target="_blank" rel="noreferrer">
            {children}
          </a>
        ),
    }),
    [],
  );
  const newestFirst = [...c.entries].reverse();
  const shown = showAll ? newestFirst : newestFirst.slice(0, SHOWN);

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <CompassIcon size={16} className="text-forest" />
          <span className="text-label font-medium">Catch me up</span>
        </div>
        <CatchUpPrice c={c} />
      </div>
      {plan && !plan.ready && plan.reason && <p className="text-label leading-relaxed text-ink-soft">{plan.reason}</p>}
      {plan?.note && <p className="mb-3 text-meta text-ink-soft">{plan.note}</p>}
      {plan?.warning && (
        <p className="mb-3 flex items-start gap-2 text-meta leading-relaxed text-sun-deep">
          <AlertIcon size={14} className="mt-0.5 shrink-0" />
          {plan.warning}
        </p>
      )}
      {error && (
        <p className="mb-3 flex items-start gap-2 text-label leading-relaxed text-trail-deep">
          <AlertIcon size={16} className="mt-0.5 shrink-0" />
          {error}
        </p>
      )}
      {!pending && shown.length === 0 && plan?.ready && (
        <p className="text-label text-ink-soft">
          Zoned out for a moment? Ask what you missed, have the last thing explained, or ask anything about the meeting
          so far.
        </p>
      )}
      {(pending || shown.length > 0) && (
        <div data-private="questions and answers" className="space-y-4">
          {pending && (
            <div className="animate-enter rounded-lg border border-line bg-surface-subtle/60 px-4 py-3">
              <div className="text-label font-medium">{pending}</div>
              <div className="mt-2 flex items-center gap-2 text-label text-ink-soft">
                <Spinner className="h-4 w-4 text-forest" /> Reading the meeting so far…
              </div>
            </div>
          )}
          {shown.map((e) => (
            <div key={e.at} className="animate-enter rounded-lg border border-line bg-surface-subtle/60 px-4 py-3">
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-label font-medium">{e.q}</span>
                <span className="shrink-0 text-meta text-ink-soft">asked at {formatDuration(e.t)}</span>
              </div>
              <div className="prose-summary mt-1.5 text-body">
                <ReactMarkdown remarkPlugins={PLUGINS} components={components}>
                  {linkTimes(e.a)}
                </ReactMarkdown>
              </div>
              <div className="mt-2 flex items-center gap-1.5 text-meta text-ink-soft">
                <SparkleIcon size={14} /> {e.label}
              </div>
            </div>
          ))}
          {newestFirst.length > SHOWN && (
            <button onClick={() => setShowAll((v) => !v)} className="btn btn-sm btn-ghost px-2 font-normal text-ink-soft">
              {showAll ? "Show fewer" : `Show ${newestFirst.length - SHOWN} earlier`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
