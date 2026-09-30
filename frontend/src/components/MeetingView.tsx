import { useEffect, useLayoutEffect, useRef } from "react";
import type { ReactNode } from "react";
import type { Bookmark, Health, Meeting, Provider, Task } from "../api";
import { formatBytes, formatDuration, formatLongDate, formatTime, tildePath } from "../format";
import AskPanel from "./AskPanel";
import { CalendarIcon, ChatIcon, ClockIcon, ExportIcon, LinesIcon, PencilIcon, SparkleIcon, TrashIcon, UsersIcon, WaveIcon } from "./icons";
import { ContourBadge } from "./illustrations";
import PipelineStatus from "./PipelineStatus";
import Summary from "./Summary";
import Transcript from "./Transcript";
import { Segmented } from "./ui";

export type Tab = "summary" | "transcript" | "ask";

interface Props {
  meeting: Meeting;
  health: Health | null;
  tab: Tab;
  onTab: (t: Tab) => void;
  onRename: (title: string) => void;
  onDelete: () => void;
  onDeleteAudio: () => void;
  onExport: () => void;
  onConfirm: () => void;
  onRetry: () => void;
  onSummarize: (p: Provider, template?: string) => void;
  onBookmarks: (b: Bookmark[]) => void;
  onSpeakers: (names: Record<string, string>) => void;
  onToggleTask: (t: Task, done: boolean) => void;
  onError: (msg: string) => void;
}

const WORKING = ["recording", "queued", "transcribing", "summarizing"];

function FileRow({ icon, title, detail, action }: { icon: ReactNode; title: string; detail: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 py-3.5">
      <div className="flex min-w-0 items-center gap-3.5">
        <span className="well">{icon}</span>
        <div className="min-w-0">
          <div className="text-label font-medium">{title}</div>
          <div className="mt-0.5 break-words text-hint text-ink-soft">{detail}</div>
        </div>
      </div>
      {action}
    </div>
  );
}

/** The meeting's title, edited in place. A textarea so long titles wrap instead of being cut off. */
function TitleField({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const fit = () => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  };
  useLayoutEffect(fit, [value]);
  useEffect(() => {
    void document.fonts?.ready.then(fit);
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, []);
  return (
    <label className="group relative -mx-2 min-w-0 flex-1">
      <span className="sr-only">Meeting title</span>
      <textarea
        ref={ref}
        key={value}
        rows={1}
        defaultValue={value}
        spellCheck={false}
        onInput={fit}
        onBlur={(e) => onSave(e.target.value.replace(/\s+/g, " ").trim())}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            e.currentTarget.blur();
          }
          if (e.key === "Escape") {
            e.currentTarget.value = value;
            fit();
            e.currentTarget.blur();
          }
        }}
        className="page-title block w-full resize-none overflow-hidden rounded-md bg-transparent px-2 py-1 pr-10 outline-none transition-[background-color,box-shadow] duration-200 hover:bg-forest/[.04] focus:bg-surface focus:shadow-focus"
      />
      <PencilIcon
        size={16}
        className="pointer-events-none absolute right-3 top-3.5 text-ink-faint opacity-0 transition-opacity group-hover:opacity-100"
      />
    </label>
  );
}

export default function MeetingView({ meeting: m, health, tab, onTab, ...on }: Props) {
  const audioGone = m.audio_deleted || m.audio_bytes === 0;

  return (
    <div key={m.id}>
      <header className="mb-8 animate-enter">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-hint text-ink-soft">
          <span className="inline-flex items-center gap-1.5">
            <CalendarIcon size={16} className="text-ink-faint" />
            {formatLongDate(m.created_at)}
          </span>
          <span className="inline-flex items-center gap-1.5 tabular-nums">
            <ClockIcon size={16} className="text-ink-faint" />
            {formatTime(m.created_at)} · {formatDuration(m.duration_sec)}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <UsersIcon size={16} className="text-ink-faint" />
            {m.has_system ? "You & them" : "One mic"}
          </span>
        </div>

        <div className="mt-2.5 flex items-start gap-2">
          <TitleField value={m.title} onSave={on.onRename} />
          <div className="mt-1 flex items-center gap-0.5">
            {m.transcript && (
              <button onClick={on.onExport} className="icon-btn" aria-label="Export" data-tip="Export now">
                <ExportIcon size={18} />
              </button>
            )}
            <button onClick={on.onDelete} className="icon-btn hover:!bg-trail-soft hover:!text-trail-deep" aria-label="Delete meeting" data-tip="Delete meeting">
              <TrashIcon size={18} />
            </button>
          </div>
        </div>
        {m.title_auto && m.summary && (
          <p className="mt-2 inline-flex items-center gap-1.5 text-hint text-ink-soft">
            <SparkleIcon size={16} className="text-sun-deep" /> Named by Trailmix · click the title to rename
          </p>
        )}
      </header>

      {m.status !== "done" && (
        <PipelineStatus meeting={m} health={health} onConfirm={on.onConfirm} onRetry={on.onRetry} onSummarize={on.onSummarize} />
      )}

      <div className="mb-4 animate-enter" style={{ animationDelay: "60ms" }}>
        <Segmented
          kind="tab"
          label="Meeting sections"
          value={tab}
          onChange={onTab}
          options={[
            { id: "summary", label: "Summary", icon: <SparkleIcon size={16} /> },
            { id: "transcript", label: "Transcript", icon: <LinesIcon size={16} /> },
            { id: "ask", label: "Ask", icon: <ChatIcon size={16} /> },
          ]}
        />
      </div>

      <div role="tabpanel" className="panel animate-enter p-5 sm:p-8" style={{ animationDelay: "100ms" }}>
        <div key={tab} className="animate-fade-in">
          {tab === "summary" && (
            <Summary meeting={m} health={health} onResummarize={(p, t) => on.onSummarize(p, t || undefined)} onToggleTask={on.onToggleTask} />
          )}
          {tab === "transcript" && (
            <Transcript
              meeting={m}
              health={health}
              pending={WORKING.includes(m.status) || m.status.startsWith("ready") || m.status === "waiting_confirm"}
              onBookmarks={on.onBookmarks}
              onSpeakers={on.onSpeakers}
            />
          )}
          {tab === "ask" &&
            (m.transcript ? (
              <AskPanel meetingId={m.id} history={m.qa} health={health} onError={on.onError} />
            ) : (
              <div className="py-8 text-center">
                <ContourBadge>
                  <ChatIcon size={18} />
                </ContourBadge>
                <p className="mt-3 text-label text-ink-soft">You can ask questions once the transcript is ready.</p>
              </div>
            ))}
        </div>
      </div>

      <section className="mt-10 animate-enter" style={{ animationDelay: "140ms" }} aria-labelledby="files-heading">
        <h2 id="files-heading" className="eyebrow mb-2 px-1">
          Files
        </h2>
        <div className="inset-surface divide-y divide-line px-4 sm:px-5">
          <FileRow
            icon={<WaveIcon size={16} />}
            title="Audio recording"
            detail={
              audioGone
                ? "Deleted. The transcript and summary are kept."
                : `${formatBytes(m.audio_bytes)}${
                    m.audio_expires_at
                      ? ` · deletes itself ${new Date(m.audio_expires_at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`
                      : " · kept until you delete it"
                  }`
            }
            action={
              !audioGone &&
              (m.transcript || m.status === "error") && (
                <button onClick={on.onDeleteAudio} className="btn btn-sm btn-danger-ghost">
                  <TrashIcon size={16} />
                  Delete audio
                </button>
              )
            }
          />
          {m.transcript && (
            <FileRow
              icon={<ExportIcon size={16} />}
              title="Export"
              detail={
                m.export_error ? (
                  <span className="text-trail-deep">{m.export_error}</span>
                ) : m.exported_paths.length > 0 ? (
                  <span className="font-mono text-meta">{m.exported_paths.map(tildePath).join("  ·  ")}</span>
                ) : (
                  "Not exported yet"
                )
              }
              action={
                <button onClick={on.onExport} className="btn btn-sm btn-soft">
                  <ExportIcon size={16} />
                  {m.exported_paths.length > 0 ? "Export again" : "Export now"}
                </button>
              }
            />
          )}
        </div>
      </section>
    </div>
  );
}
