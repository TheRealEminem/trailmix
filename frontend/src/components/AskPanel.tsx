import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import type { Health, QA } from "../api";
import { ArrowUpIcon, ChatIcon, SparkleIcon, TrashIcon } from "./icons";
import { ContourBadge } from "./illustrations";
import { Spinner } from "./ui";

interface Entry {
  q: string;
  a: string;
  provider: string;
  sources?: { id: number; title: string }[];
}

interface Props {
  /** Ask one meeting (with its saved history), or omit to ask across every meeting. */
  meetingId?: number;
  /** Asking across meetings: only those in this workspace. */
  workspaceId?: number | null;
  history?: QA[];
  health: Health | null;
  onOpenMeeting?: (id: number) => void;
  onError: (msg: string) => void;
  onCleared?: () => void;
}

const MEETING_PROMPTS = ["What was decided?", "What did they commit to, and by when?", "Write a 3-bullet update I can paste in Slack"];
const ALL_PROMPTS = ["What did I promise to do recently?", "When did we last talk about pricing?", "What's still unresolved across my meetings?"];

export default function AskPanel({ meetingId, workspaceId = null, history = [], health, onOpenMeeting, onError, onCleared }: Props) {
  const [entries, setEntries] = useState<Entry[]>(history);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<string | null>(null); // the question being answered
  const busy = pending !== null;
  const endRef = useRef<HTMLDivElement>(null);
  const perMeeting = meetingId !== undefined;
  const provider = health?.summary;
  const label = (id: string) => health?.providers.find((p) => p.id === id)?.label ?? id;

  useEffect(() => {
    setEntries(history);
  }, [meetingId]); // eslint-disable-line react-hooks/exhaustive-deps
  // Braces matter: scrollIntoView returns a Promise in newer browsers, and an effect must not return one.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [entries.length, busy]);

  const ask = async (q: string) => {
    q = q.trim();
    if (!q || busy) return;
    setPending(q);
    setQuestion("");
    try {
      if (perMeeting) {
        const e = await api.askMeeting(meetingId, q);
        setEntries((xs) => [...xs, e]);
      } else {
        const r = await api.askAll(q, workspaceId);
        setEntries((xs) => [...xs, { q, a: r.answer, provider: r.provider, sources: r.sources }]);
      }
    } catch (e) {
      setQuestion(q);
      onError((e as Error).message);
    } finally {
      setPending(null);
    }
  };

  const clear = async () => {
    if (perMeeting) await api.clearQuestions(meetingId);
    setEntries([]);
    onCleared?.();
  };

  // [M12] citations become links to that meeting.
  const linkify = (text: string, sources?: Entry["sources"]) =>
    text.replace(/\[M(\d+)\]/g, (_, id) => {
      const title = sources?.find((s) => s.id === Number(id))?.title ?? `Meeting ${id}`;
      return `[${title}](#meeting-${id})`;
    });

  return (
    <div>
      {entries.length === 0 && !busy && (
        <div className="py-4 text-center">
          <ContourBadge>
            <ChatIcon size={18} />
          </ContourBadge>
          <p className="mt-3 font-medium">{perMeeting ? "Ask this meeting anything" : "Ask across all your meetings"}</p>
          <p className="mx-auto mt-1 max-w-md text-label leading-relaxed text-ink-soft">
            Answered by {provider ? `${provider.label}${provider.local ? " on this machine" : " (cloud)"}` : "your summary provider"}, using
            {perMeeting ? " this meeting's transcript and notes." : " the meetings that best match your question."}
          </p>
          <div className="mt-5 flex flex-wrap justify-center gap-2">
            {(perMeeting ? MEETING_PROMPTS : ALL_PROMPTS).map((p) => (
              <button key={p} onClick={() => void ask(p)} className="btn btn-sm btn-soft rounded-full px-3.5 font-normal">
                {p}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="space-y-6">
        {entries.map((e, i) => (
          <div key={i} className="animate-enter space-y-2.5">
            <div className="flex justify-end">
              <div className="max-w-[85%] rounded-lg rounded-br-xs bg-forest px-4 py-2.5 text-body leading-relaxed text-on-accent shadow-sm">{e.q}</div>
            </div>
            <div className="max-w-[94%] rounded-lg rounded-bl-xs border border-line bg-surface-subtle/60 px-4 py-3">
              <div className="prose-summary text-body">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    a: ({ href, children }) =>
                      href?.startsWith("#meeting-") ? (
                        <button
                          onClick={() => onOpenMeeting?.(Number(href.slice(9)))}
                          className="chip mx-0.5 bg-sun-soft align-middle text-sun-deep no-underline transition-colors hover:bg-sun/30"
                        >
                          {children}
                        </button>
                      ) : (
                        <a href={href} target="_blank" rel="noreferrer">
                          {children}
                        </a>
                      ),
                  }}
                >
                  {linkify(e.a, e.sources)}
                </ReactMarkdown>
              </div>
              <div className="mt-2 flex items-center gap-1.5 text-meta text-ink-soft">
                <SparkleIcon size={16} /> {label(e.provider)}
                {e.sources && e.sources.length > 0 && <span>· searched {e.sources.length} meetings</span>}
              </div>
            </div>
          </div>
        ))}
        {pending && (
          <div className="flex animate-enter justify-end">
            <div className="max-w-[85%] rounded-lg rounded-br-xs bg-forest/80 px-4 py-2.5 text-body text-on-accent">{pending}</div>
          </div>
        )}
        {busy && (
          <div className="flex items-center gap-2 text-label text-ink-soft">
            <Spinner className="h-4 w-4 text-forest" /> Thinking it over…
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void ask(question);
        }}
        className="mt-7 flex items-center gap-2"
      >
        <div className="field flex flex-1 items-center gap-2 py-1 pl-4 pr-1 focus-within:border-[color:color-mix(in_srgb,var(--forest)_55%,transparent)] focus-within:shadow-focus">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder={perMeeting ? "Ask about this meeting…" : "Ask about any of your meetings…"}
            className="h-9 min-w-0 flex-1 bg-transparent text-body outline-none"
            disabled={busy}
            aria-label="Question"
          />
          <button
            type="submit"
            disabled={busy || !question.trim()}
            className="btn btn-primary h-8 w-8 shrink-0 rounded-sm p-0"
            aria-label="Ask"
            data-tip="Ask (Enter)"
          >
            <ArrowUpIcon size={16} strokeWidth={2.2} />
          </button>
        </div>
        {entries.length > 0 && (
          <button type="button" onClick={() => void clear()} className="icon-btn" aria-label="Clear conversation" data-tip="Clear conversation">
            <TrashIcon size={16} />
          </button>
        )}
      </form>
    </div>
  );
}
