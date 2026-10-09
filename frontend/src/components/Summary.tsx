import { useState } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Health, Meeting, Provider, Task } from "../api";
import { AlertIcon, CheckIcon, CompassIcon, FlagIcon, LeafIcon, RefreshIcon, SignpostIcon, SparkleIcon } from "./icons";
import { ContourBadge } from "./illustrations";
import TaskItem from "./TaskItem";
import { CopyButton, ProviderSelect, TemplateSelect } from "./ui";

interface Props {
  meeting: Meeting;
  health: Health | null;
  onResummarize: (provider: Provider, template: string) => void;
  onToggleTask: (task: Task, done: boolean) => void;
}

/** Splits the summary around its "## Action Items" section so that part can be interactive. */
function splitActions(md: string): { before: string; heading: string | null; after: string } {
  const lines = md.split("\n");
  const start = lines.findIndex((l) => /^#{1,4}\s*action items\b/i.test(l));
  if (start < 0) return { before: md, heading: null, after: "" };
  let end = lines.findIndex((l, i) => i > start && /^#{1,4}\s/.test(l));
  if (end < 0) end = lines.length;
  return {
    before: lines.slice(0, start).join("\n"),
    heading: lines[start].replace(/^#+\s*/, ""),
    after: lines.slice(end).join("\n"),
  };
}

// Each summary section gets its own little trail marker.
function headingIcon(text: string) {
  const t = text.toLowerCase();
  if (t.includes("overview")) return <CompassIcon size={16} />;
  if (t.includes("key")) return <LeafIcon size={16} />;
  if (t.includes("decision")) return <SignpostIcon size={16} />;
  if (t.includes("action")) return <CheckIcon size={16} />;
  if (t.includes("flag")) return <FlagIcon size={16} />;
  return <SparkleIcon size={16} />;
}

const HeadingIcon = ({ children }: { children: ReactNode }) => <span className="well well-forest h-7 w-7">{children}</span>;

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  return "";
}

export default function Summary({ meeting, health, onResummarize, onToggleTask }: Props) {
  const [provider, setProvider] = useState<Provider>("auto");
  const [template, setTemplate] = useState("");
  const busy = !["done", "error"].includes(meeting.status);
  const canRegenerate = !!meeting.transcript && meeting.status !== "ready_summarize";

  return (
    <div data-private="notes">
      {meeting.summary_error && (
        <div className="mb-6 flex items-start gap-3 rounded-lg border border-trail/20 bg-trail-soft/70 p-4 text-label text-trail-deep">
          <AlertIcon size={18} className="mt-0.5 shrink-0" />
          <div>
            <div className="font-semibold">The summary didn't come through</div>
            <div className="mt-0.5 break-words opacity-90">{meeting.summary_error}</div>
          </div>
        </div>
      )}

      {meeting.summary ? (
        <SummaryBody summary={meeting.summary} tasks={meeting.tasks} onToggleTask={onToggleTask} />
      ) : (
        !meeting.summary_error && (
          <div className="py-8 text-center">
            <ContourBadge>
              <SparkleIcon size={18} />
            </ContourBadge>
            <p className="mt-3 font-medium">No summary yet</p>
            <p className="mx-auto mt-1 max-w-sm text-label leading-relaxed text-ink-soft">
              {!meeting.transcript
                ? busy
                  ? "First the transcript, then the summary. It will appear here when it's ready."
                  : "There's no transcript to summarize."
                : busy
                  ? "It will appear here as soon as it's ready."
                  : "Use Summarize below to write one from the transcript."}
            </p>
          </div>
        )
      )}

      {canRegenerate && (
        <div className="mt-10 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-5">
          <div className="flex items-center gap-2">
            {meeting.summary && <CopyButton text={meeting.summary} />}
            {meeting.summary_provider && (
              <span className="text-hint text-ink-soft">
                Written by{" "}
                <span className="font-medium text-ink">
                  {health?.providers.find((p) => p.id === meeting.summary_provider)?.label ?? meeting.summary_provider}
                </span>
                {meeting.summary_model && meeting.summary_model.toLowerCase() !== meeting.summary_provider.toLowerCase() && (
                  <span className="font-mono"> · {meeting.summary_model}</span>
                )}
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <TemplateSelect value={template} onChange={setTemplate} health={health} disabled={busy} />
            <ProviderSelect value={provider} onChange={setProvider} health={health} disabled={busy} />
            <button onClick={() => onResummarize(provider, template)} disabled={busy} className="btn btn-sm btn-soft">
              <RefreshIcon size={16} />
              {meeting.summary ? "Regenerate" : "Summarize"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        h2: ({ children }) => (
          <h2>
            <HeadingIcon>{headingIcon(textOf(children))}</HeadingIcon>
            {children}
          </h2>
        ),
      }}
    >
      {children}
    </ReactMarkdown>
  );
}

function SummaryBody({ summary, tasks, onToggleTask }: { summary: string; tasks: Task[]; onToggleTask: (t: Task, done: boolean) => void }) {
  const { before, heading, after } = splitActions(summary);
  if (!heading || tasks.length === 0) {
    return (
      <div className="prose-summary">
        <Markdown>{summary}</Markdown>
      </div>
    );
  }
  const open = tasks.filter((t) => !t.done).length;
  return (
    <div className="prose-summary">
      <Markdown>{before}</Markdown>
      <h2>
        <HeadingIcon>
          <CheckIcon size={16} />
        </HeadingIcon>
        {heading}
        <span className="chip ml-0.5 bg-surface-subtle text-ink-soft">{open ? `${open} open` : "all done"}</span>
      </h2>
      <div className="-mx-2 space-y-px">
        {tasks.map((t) => (
          <TaskItem key={t.id} text={t.text} done={t.done} onToggle={(d) => onToggleTask(t, d)} />
        ))}
      </div>
      {after && <Markdown>{after}</Markdown>}
    </div>
  );
}
