import { useState } from "react";
import type { ReactNode } from "react";
import type { Health, Meeting, Provider } from "../api";
import { shortModel } from "../format";
import { AlertIcon, CheckIcon, LinesIcon, MicIcon, RefreshIcon, SignpostIcon, SparkleIcon } from "./icons";
import { ProviderSelect, Spinner, TemplateSelect } from "./ui";

type StepState = "done" | "active" | "waiting" | "todo" | "error";

interface Props {
  meeting: Meeting;
  health: Health | null;
  onConfirm: () => void;
  onRetry: () => void;
  onSummarize: (p: Provider, template?: string) => void;
}

function stepStates(m: Meeting): [StepState, StepState, StepState] {
  const hasTranscript = !!m.transcript;
  switch (m.status) {
    case "recording":
      return ["active", "todo", "todo"];
    case "queued":
      return hasTranscript ? ["done", "done", "active"] : ["done", "active", "todo"];
    case "ready_transcribe":
      return ["done", "waiting", "todo"];
    case "transcribing":
      return ["done", "active", "todo"];
    case "ready_summarize":
      return ["done", "done", "waiting"];
    case "waiting_confirm":
      return hasTranscript ? ["done", "done", "waiting"] : ["done", "waiting", "todo"];
    case "summarizing":
      return ["done", "done", "active"];
    case "error":
      return hasTranscript ? ["done", "done", "error"] : ["done", "error", "todo"];
    default:
      return ["done", "done", "done"];
  }
}

/** "Transcribing mic track: 3/12" -> { track: "mic", done: 3, total: 12 } */
function parseProgress(reason: string | null) {
  const m = reason?.match(/(\w+) track: (\d+)\/(\d+)/);
  return m ? { track: m[1] === "mic" ? "your mic" : "meeting audio", done: +m[2], total: +m[3] } : null;
}

const STEPS = [
  { label: "Recorded", icon: MicIcon },
  { label: "Transcript", icon: LinesIcon },
  { label: "Summary", icon: SparkleIcon },
];

function Waypoint({ state, label, Icon, sub }: { state: StepState; label: string; Icon: typeof MicIcon; sub: string }) {
  const look = {
    done: "bg-forest text-on-accent",
    active: "bg-sky-soft text-sky-deep ring-1 ring-sky/30",
    waiting: "bg-sun-soft text-sun-deep ring-1 ring-sun/40",
    todo: "bg-surface text-ink-faint ring-1 ring-line-strong",
    error: "bg-trail-soft text-trail-deep ring-1 ring-trail/30",
  }[state];
  return (
    <li className="relative z-10 flex flex-1 flex-col items-center text-center">
      <div className={`relative flex h-9 w-9 items-center justify-center rounded-full transition-colors duration-300 ${look}`}>
        {(state === "active" || state === "waiting") && (
          <span className={`absolute inset-0 animate-ring-pulse rounded-full ${state === "active" ? "bg-sky/30" : "bg-sun/40"}`} />
        )}
        {state === "done" ? <CheckIcon size={16} strokeWidth={2.4} /> : state === "error" ? <AlertIcon size={16} /> : <Icon size={16} />}
      </div>
      <div className={`mt-2 text-hint font-medium ${state === "todo" ? "text-ink-soft" : "text-ink"}`}>{label}</div>
      <div className="mt-0.5 h-4 text-meta tabular-nums text-ink-soft">{sub}</div>
    </li>
  );
}

export default function PipelineStatus({ meeting: m, health, onConfirm, onRetry, onSummarize }: Props) {
  const [provider, setProvider] = useState<Provider>("auto");
  const [template, setTemplate] = useState("");
  const states = stepStates(m);
  const progress = parseProgress(m.wait_reason);
  const reached = states[2] === "done" ? 100 : states[1] === "done" ? 50 : 0;

  const sub = (s: StepState, i: number) => {
    if (s === "done") return i === 0 ? "" : "Done";
    if (s === "waiting") return m.status === "waiting_confirm" ? "Low memory" : "Your call";
    if (s === "error") return "Hit a snag";
    if (s === "active") {
      if (m.status === "queued") return m.wait_reason ? "After recording" : "Queued";
      if (progress) return `${progress.done} of ${progress.total}`;
      return m.status === "recording" ? "Live" : "Working…";
    }
    return "";
  };

  return (
    <div className="panel mb-8 animate-enter overflow-hidden" style={{ animationDelay: "40ms" }}>
      <div className="px-4 pb-4 pt-5 sm:px-6">
        <div className="relative">
          {/* the trail between waypoints */}
          <div aria-hidden="true" className="absolute left-[16.67%] right-[16.67%] top-[17px] h-0.5 rounded-full bg-line-strong">
            <div className="h-full rounded-full bg-forest transition-[width] duration-700 ease-out" style={{ width: `${reached}%` }} />
          </div>
          <ol className="relative flex" aria-label="Progress">
            {STEPS.map((step, i) => (
              <Waypoint key={step.label} state={states[i]} label={step.label} Icon={step.icon} sub={sub(states[i], i)} />
            ))}
          </ol>
        </div>
      </div>

      <div className="border-t border-line bg-surface-subtle/40 px-5 py-4 text-label sm:px-6">
        {m.status === "ready_transcribe" && (
          <ActionRow
            icon={<SignpostIcon size={18} />}
            tone="sun"
            title="Recording saved. Ready to transcribe?"
            body={
              health?.transcription.engine === "remote"
                ? `Sends the audio to your transcription endpoint (${health.transcription.url}).`
                : "Runs the full-quality model (about 3 GB of RAM) and frees it when it's finished."
            }
          >
            <button onClick={onConfirm} className="btn btn-md btn-primary">
              <LinesIcon size={16} />
              Generate transcript
            </button>
          </ActionRow>
        )}

        {m.status === "ready_summarize" && (
          <ActionRow
            icon={<SignpostIcon size={18} />}
            tone="sun"
            title="Transcript's done. Summarize it?"
            body="Writes notes, action items and a title. Pick a template for the kind of meeting it was."
          >
            <div className="flex flex-wrap items-center gap-2">
              <TemplateSelect value={template} onChange={setTemplate} health={health} />
              <ProviderSelect value={provider} onChange={setProvider} health={health} />
              <button onClick={() => onSummarize(provider, template || undefined)} className="btn btn-md btn-primary">
                <SparkleIcon size={16} />
                Summarize
              </button>
            </div>
          </ActionRow>
        )}

        {m.status === "waiting_confirm" && (
          <ActionRow icon={<AlertIcon size={18} />} tone="sun" title="Running low on memory" body={m.wait_reason ?? ""}>
            <button onClick={onConfirm} className="btn btn-md btn-sun">
              Proceed anyway
            </button>
          </ActionRow>
        )}

        {m.status === "error" && (
          <ActionRow icon={<AlertIcon size={18} />} tone="trail" title="Something went wrong" body={m.error ?? "Unknown error"}>
            {!m.transcript && (
              <button onClick={onRetry} className="btn btn-md btn-soft">
                <RefreshIcon size={16} />
                Try again
              </button>
            )}
          </ActionRow>
        )}

        {(m.status === "queued" || m.status === "transcribing" || m.status === "summarizing" || m.status === "recording") && (
          <div className="flex items-start gap-3 text-ink-soft">
            <Spinner className="mt-[3px] h-4 w-4 shrink-0 text-sky" />
            <span className="leading-relaxed">
              <span className="text-ink">
                {m.status === "recording" && "Recording in progress."}
                {m.status === "queued" && (m.wait_reason ?? "Queued. Starting shortly.")}
                {m.status === "transcribing" &&
                  (progress
                    ? `Transcribing ${progress.track} with ${shortModel(health?.transcription.model ?? "the full model")}…`
                    : `Transcribing with ${shortModel(health?.transcription.model ?? "the full model")}…`)}
                {m.status === "summarizing" && `Writing the summary with ${health?.summary.label ?? "your provider"}…`}
              </span>{" "}
              You can keep browsing; this runs in the background.
            </span>
          </div>
        )}
        {m.status === "transcribing" && progress && (
          <div className="ml-7 mt-3 h-1 overflow-hidden rounded-full bg-[var(--switch-off)]">
            <div className="h-full rounded-full bg-sky transition-[width] duration-500 ease-out" style={{ width: `${(progress.done / progress.total) * 100}%` }} />
          </div>
        )}
      </div>
    </div>
  );
}

function ActionRow({ icon, tone, title, body, children }: { icon: ReactNode; tone: "sun" | "trail"; title: string; body: string; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-4">
      <div className="flex min-w-0 flex-1 basis-72 items-start gap-3">
        <span className={`well ${tone === "sun" ? "bg-sun-soft text-sun-deep" : "bg-trail-soft text-trail-deep"}`}>{icon}</span>
        <div className="min-w-0">
          <div className="font-semibold">{title}</div>
          <div className="mt-0.5 break-words leading-relaxed text-ink-soft">{body}</div>
        </div>
      </div>
      {children}
    </div>
  );
}
