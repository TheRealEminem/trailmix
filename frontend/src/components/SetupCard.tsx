import { useEffect, useState } from "react";
import { api } from "../api";
import type {
  Health,
  NativeRecorder,
  OllamaStatus,
  SpeechModels,
} from "../api";
import { CheckIcon, CloseIcon } from "./icons";

const DISMISSED_KEY = "trailmix.setupDismissed";
// Instruction-following families that write good notes (see llm_engine.preferred_ollama_model).
const GOOD_MODELS = /^(qwen3|qwen2\.5|gemma3|llama3\.[12]|mistral|phi4)/i;

interface Props {
  health: Health | null;
  native: NativeRecorder | null;
  onOpenSettings: () => void;
  onChanged: () => void;
}

function Step({
  done,
  title,
  children,
}: {
  done: boolean;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <li className="flex gap-3 py-3">
      <span
        className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${
          done ? "bg-forest text-white" : "ring-1 ring-inset ring-line-strong"
        }`}
        aria-hidden="true"
      >
        {done && <CheckIcon size={12} strokeWidth={3} />}
      </span>
      <div className="min-w-0 flex-1">
        <div
          className={`text-label font-medium ${done ? "text-ink-soft" : "text-ink"}`}
        >
          {title}
        </div>
        {!done && children && (
          <div className="mt-1.5 text-hint leading-relaxed text-ink-soft">
            {children}
          </div>
        )}
      </div>
    </li>
  );
}

/**
 * First run: a short checklist on the home screen (your name, microphone access, speech models, a summary
 * model). It disappears once everything is done, or when you close it.
 */
export default function SetupCard({
  health,
  native,
  onOpenSettings,
  onChanged,
}: Props) {
  const [dismissed, setDismissed] = useState(() => {
    try {
      return localStorage.getItem(DISMISSED_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [name, setName] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [speech, setSpeech] = useState<SpeechModels | null>(null);
  const [ollama, setOllama] = useState<OllamaStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const pulling = !!ollama?.pull?.active;
  useEffect(() => {
    if (dismissed) return;
    let alive = true;
    const load = () => {
      api
        .getSettings()
        .then((s) => alive && setName(s.your_name))
        .catch(() => undefined);
      api
        .speechModels()
        .then((m) => alive && setSpeech(m))
        .catch(() => undefined);
      api
        .ollama()
        .then((o) => alive && setOllama(o))
        .catch(() => undefined);
    };
    load();
    const t = setInterval(load, pulling ? 1500 : 6000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [dismissed, pulling]);

  useEffect(() => {
    if (ollama?.pull && !ollama.pull.active && ollama.pull.progress === 1)
      onChanged(); // a new model is ready
  }, [ollama?.pull?.active]); // eslint-disable-line react-hooks/exhaustive-deps

  if (dismissed || name === null || !health) return null;

  const nameDone = name.trim() !== "";
  const micDone = !native || native.mic_allowed !== false;
  const speechDone =
    !speech || !speech.local || speech.models.every((m) => m.installed);
  const usingOllama = health.summary.id === "ollama";
  const summaryDone = usingOllama
    ? !!ollama?.reachable && ollama.models.some((m) => GOOD_MODELS.test(m))
    : health.summary.ready;
  if (nameDone && micDone && speechDone && summaryDone) return null;

  const dismiss = () => {
    setDismissed(true);
    try {
      localStorage.setItem(DISMISSED_KEY, "1");
    } catch {
      /* ignore */
    }
  };
  const saveName = () => {
    const v = draft.trim();
    if (!v) return;
    api.putSettings({ your_name: v }).then(() => {
      setName(v);
      onChanged();
    });
  };
  const pull = () => {
    setError(null);
    api
      .pullOllamaModel()
      .then(setOllama)
      .catch((e) => setError((e as Error).message));
  };
  const pct = ollama?.pull?.progress;

  return (
    <section
      aria-label="Get set up"
      className="glass mb-6 animate-enter overflow-hidden"
    >
      <header className="flex items-start justify-between gap-3 px-5 pt-4 sm:px-6">
        <div>
          <h2 className="font-display text-lead font-semibold tracking-[-0.01em]">
            Get set up
          </h2>
          <p className="mt-0.5 text-hint text-ink-soft">
            A few things so your first meeting comes out right.
          </p>
        </div>
        <button
          onClick={dismiss}
          className="icon-btn h-8 w-8"
          aria-label="Hide setup"
          data-tip="Hide"
        >
          <CloseIcon size={16} />
        </button>
      </header>
      <ol className="divide-y divide-line px-5 pb-1 sm:px-6">
        <Step
          done={nameDone}
          title={nameDone ? `Your name: ${name}` : "Your name"}
        >
          Notes and action items use it instead of “You”.
          <div className="mt-2 flex gap-2">
            <input
              className="field h-8 w-48 px-2.5 text-label"
              placeholder="Your name"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && saveName()}
              aria-label="Your name"
            />
            <button
              className="btn btn-sm btn-soft"
              onClick={saveName}
              disabled={!draft.trim()}
            >
              Save
            </button>
          </div>
        </Step>
        {native && (
          <Step
            done={micDone}
            title={micDone ? "Microphone allowed" : "Allow the microphone"}
          >
            Trailmix needs it to record your side.{" "}
            <a
              className="font-medium text-forest underline underline-offset-2"
              href="x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
            >
              Open Privacy & Security → Microphone
            </a>{" "}
            and turn on Trailmix. When you record a call, also allow System
            Audio Recording when macOS asks.
          </Step>
        )}
        <Step
          done={speechDone}
          title={
            speechDone ? "Speech models ready" : "Speech models downloading"
          }
        >
          About 1.7 GB, once. You can record meanwhile; transcripts start when
          it's done.
        </Step>
        <Step
          done={summaryDone}
          title={
            summaryDone
              ? `Summaries: ${usingOllama ? ollama?.using || "local model" : health.summary.label}`
              : "Set up summaries"
          }
        >
          {usingOllama && !ollama?.reachable && (
            <>
              Summaries run on this Mac with Ollama (free), or in the cloud with
              an API key.
              <div className="mt-2 flex flex-wrap gap-2">
                <a
                  className="btn btn-sm btn-soft"
                  href="https://ollama.com/download"
                >
                  Get Ollama
                </a>
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={onOpenSettings}
                >
                  Use an API key instead
                </button>
              </div>
            </>
          )}
          {usingOllama && ollama?.reachable && (
            <>
              {pulling ? (
                <>
                  Downloading {ollama.pull!.model}
                  {pct != null ? ` · ${Math.round(pct * 100)}%` : "…"}
                  <div
                    className="mt-2 h-1.5 overflow-hidden rounded-full bg-forest/10"
                    aria-hidden="true"
                  >
                    <div
                      className="h-full rounded-full bg-forest transition-[width] duration-700"
                      style={{ width: `${Math.max(2, (pct ?? 0) * 100)}%` }}
                    />
                  </div>
                </>
              ) : (
                <>
                  {ollama.models.length
                    ? `Your installed models (${ollama.models.join(", ")}) aren't great at meeting notes.`
                    : "Ollama has no models yet."}{" "}
                  {ollama.recommended} writes much better notes and suits this
                  Mac. Cloud models (Claude, OpenAI, Gemini) are better still.
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button className="btn btn-sm btn-soft" onClick={pull}>
                      Download {ollama.recommended}
                    </button>
                    <button
                      className="btn btn-sm btn-ghost"
                      onClick={onOpenSettings}
                    >
                      Use an API key instead
                    </button>
                  </div>
                  {(error || ollama.pull?.error) && (
                    <p className="mt-1.5 text-trail-deep">
                      {error || ollama.pull?.error}
                    </p>
                  )}
                </>
              )}
            </>
          )}
          {!usingOllama && (
            <>
              {health.summary.reason ||
                `Add your ${health.summary.label} API key.`}
              <div className="mt-2">
                <button
                  className="btn btn-sm btn-soft"
                  onClick={onOpenSettings}
                >
                  Open Settings
                </button>
              </div>
            </>
          )}
        </Step>
      </ol>
    </section>
  );
}
