import { useEffect, useMemo, useRef, useState } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import type { CatchUpEntry, CatchUpKind, CatchUpPlan } from "../api";
import { formatDuration } from "../format";
import { AlertIcon, ArrowUpIcon, CompassIcon, SparkleIcon } from "./icons";
import { Spinner } from "./ui";

interface Props {
  /** The meeting being recorded, whichever app captures it. */
  meetingId: number;
  /** Shows that moment in the live transcript (a timestamp in an answer was clicked). */
  onSeek: (seconds: number) => void;
}

const QUICK: { kind: Exclude<CatchUpKind, "question">; label: string; tip: string }[] = [
  { kind: "missed", label: "What did I miss?", tip: "The gist of the last five minutes" },
  { kind: "explain", label: "Explain that", tip: "The last thing said, in plainer words" },
  { kind: "decided", label: "What's been decided?", tip: "Decisions so far, and what's still open" },
];
const PLAN_REFRESH_MS = 30_000; // the price grows with the transcript
const SHOWN = 3;
const PLUGINS = [remarkGfm];

function price(plan: CatchUpPlan) {
  if (plan.local) return "on this Mac · free";
  if (plan.cost === null) return "paid per question (price not known to Trailmix)";
  if (plan.cost < 0.01) return "under 1¢ a question";
  return `about $${plan.cost < 0.1 ? plan.cost.toFixed(3) : plan.cost.toFixed(2)} a question`;
}

/** [12:34] and [1:02:03] in an answer become links that show that moment in the live transcript. */
const linkTimes = (text: string) =>
  text.replace(/\[((?:\d+:)?\d{1,2}:\d\d)\](?!\()/g, (_, t: string) => {
    const secs = t.split(":").reduce((acc, n) => acc * 60 + Number(n), 0);
    return `[${t}](#t-${secs})`;
  });

/** "Catch me up": quick questions about the meeting so far, while it's still recording (backend/catch_up.py). */
export default function CatchUp({ meetingId, onSeek }: Props) {
  const [plan, setPlan] = useState<CatchUpPlan | null>(null);
  const [entries, setEntries] = useState<CatchUpEntry[]>([]);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const busy = pending !== null;
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

  const ready = !!plan?.ready;
  const newestFirst = [...entries].reverse();
  const shown = showAll ? newestFirst : newestFirst.slice(0, SHOWN);

  return (
    <div className="border-t border-line px-5 py-5 sm:px-6" data-testid="catch-up">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <CompassIcon size={16} className="text-forest" />
          <span className="text-label font-medium">Catch me up</span>
        </div>
        {plan?.ready && (
          <span className="text-meta text-ink-soft">
            {plan.label}
            {plan.model ? ` · ${plan.model}` : ""} · {price(plan)}
          </span>
        )}
      </div>

      {plan && !plan.ready && plan.reason && <p className="text-label leading-relaxed text-ink-soft">{plan.reason}</p>}
      {plan?.note && <p className="mb-3 text-meta text-ink-soft">{plan.note}</p>}
      {plan?.warning && (
        <p className="mb-3 flex items-start gap-2 text-meta leading-relaxed text-sun-deep">
          <AlertIcon size={14} className="mt-0.5 shrink-0" />
          {plan.warning}
        </p>
      )}

      {(ready || !plan) && (
        <>
          <div className="flex flex-wrap gap-2">
            {QUICK.map((q) => {
              const off = plan?.off[q.kind];
              return (
                <button
                  key={q.kind}
                  onClick={() => void ask(q.kind, q.label)}
                  disabled={busy || !ready || !!off}
                  className="btn btn-sm btn-soft rounded-full px-3.5 font-normal"
                  data-tip={off ?? q.tip}
                >
                  {q.label}
                </button>
              );
            })}
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const q = question.trim();
              if (q) void ask("question", q, q);
            }}
            className="mt-3"
          >
            <div className="field flex items-center gap-2 py-1 pl-4 pr-1 focus-within:border-[color:color-mix(in_srgb,var(--forest)_55%,transparent)] focus-within:shadow-focus">
              <input
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="Ask about the meeting so far…"
                className="h-9 min-w-0 flex-1 bg-transparent text-body outline-none"
                disabled={busy || !ready}
                aria-label="Question about the meeting so far"
              />
              <button
                type="submit"
                disabled={busy || !ready || !question.trim()}
                className="btn btn-primary h-8 w-8 shrink-0 rounded-sm p-0"
                aria-label="Ask"
                data-tip="Ask (Enter)"
              >
                <ArrowUpIcon size={16} strokeWidth={2.2} />
              </button>
            </div>
          </form>
        </>
      )}

      {error && (
        <p className="mt-3 flex items-start gap-2 text-label leading-relaxed text-trail-deep">
          <AlertIcon size={16} className="mt-0.5 shrink-0" />
          {error}
        </p>
      )}

      {(busy || shown.length > 0) && (
        <div data-private="questions and answers" className="mt-5 space-y-4">
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
