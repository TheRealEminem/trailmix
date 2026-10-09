import { useEffect, useState } from "react";
import { api } from "../api";
import type { MeetingQuestion } from "../api";
import { CloseIcon, SparkleIcon } from "./icons";

/**
 * "A few quick questions": what the AI wasn't sure about in this meeting (who was on the call, a name that
 * looks misspelled, a word that may have been misheard, which workspace it belongs in). One tap fixes the
 * meeting, and names and terms you confirm are spelled right in future meetings.
 */
export default function Questions({
  meetingId,
  version,
  onAnswered,
}: {
  meetingId: number;
  /** Changes when the meeting does (e.g. its notes were rewritten): look for new questions. */
  version: string;
  onAnswered: () => void;
}) {
  const [items, setItems] = useState<MeetingQuestion[]>([]);
  const [typing, setTyping] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .questions(meetingId)
      .then((q) => alive && setItems(q))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [meetingId, version]);

  if (!items.length) return null;

  const done = async (q: MeetingQuestion, value: string | null) => {
    setBusy(q.id);
    try {
      if (value === null) await api.dismissQuestion(q.id);
      else await api.answerQuestion(q.id, value);
      setItems((xs) => xs.filter((x) => x.id !== q.id));
      if (value !== null) onAnswered();
    } finally {
      setBusy(null);
    }
  };

  return (
    <section
      className="mb-6 animate-enter rounded-lg border border-sun/30 bg-sun-soft/40 px-4 py-3.5 sm:px-5"
      aria-label="Quick questions"
      data-private="questions about this meeting"
    >
      <div className="mb-2 flex items-center gap-2 text-label font-medium">
        <SparkleIcon size={16} className="text-sun-deep" />
        {items.length === 1 ? "A quick question" : "A few quick questions"}
        <span className="font-normal text-ink-soft">· your answers fix this meeting and help next time</span>
      </div>
      <ul className="divide-y divide-sun/20">
        {items.map((q) => (
          <li key={q.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 py-2.5">
            <span className="min-w-0 flex-1 basis-56 text-ui">{q.prompt}</span>
            <span className="flex flex-wrap items-center gap-1.5">
              {q.options.map((o) => (
                <button
                  key={o.value}
                  className="btn btn-sm btn-soft rounded-full px-3"
                  disabled={busy === q.id}
                  onClick={() => void done(q, o.value)}
                >
                  {o.label}
                </button>
              ))}
              {q.free_text && (
                <form
                  className="flex items-center gap-1.5"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const v = (typing[q.id] ?? "").trim();
                    if (v) void done(q, v);
                  }}
                >
                  <input
                    value={typing[q.id] ?? ""}
                    onChange={(e) => setTyping((t) => ({ ...t, [q.id]: e.target.value }))}
                    placeholder={q.kind === "them" ? "Someone else…" : "Something else…"}
                    aria-label="Your answer"
                    spellCheck
                    className="field h-8 w-36 py-0 text-ui"
                  />
                </form>
              )}
              <button
                className="icon-btn h-7 w-7"
                aria-label="Don't ask about this"
                data-tip="Don't ask about this"
                disabled={busy === q.id}
                onClick={() => void done(q, null)}
              >
                <CloseIcon size={14} />
              </button>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
