import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type {
  DeviceAdvice,
  Health,
  NativeRecorder,
  OllamaStatus,
  SpeechModels,
  Workspace,
} from "../api";
import { CheckIcon, CloseIcon } from "./icons";
import SoundCheck, { soundCheckPassed } from "./SoundCheck";
import { OrganizeProgress, WorkspaceSuggestions, useOrganize } from "./Workspaces";

const DISMISSED_KEY = "trailmix.setupDismissed";
const WORKSPACES_SKIPPED_KEY = "trailmix.workspacesSkipped";
// Instruction-following families that write good notes (see llm_engine.preferred_ollama_model).
const GOOD_MODELS = /^(qwen3|qwen2\.5|gemma3|llama3\.[12]|mistral|phi4)/i;

interface Props {
  health: Health | null;
  /** At least one meeting exists: the last step ("record your first meeting") is done. */
  hasMeetings: boolean;
  /** How many meetings there are (workspace suggestions need a few). */
  meetingCount: number;
  workspaces: Workspace[];
  native: NativeRecorder | null;
  onOpenSettings: (section?: string) => void;
  onChanged: () => void;
}

function remembered(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function remember(key: string) {
  try {
    localStorage.setItem(key, "1");
  } catch {
    /* private window */
  }
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
  hasMeetings,
  meetingCount,
  workspaces,
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
  const [device, setDevice] = useState<DeviceAdvice | null>(null);
  const [skippedSpaces, setSkippedSpaces] = useState(() => remembered(WORKSPACES_SKIPPED_KEY));
  const organize = useOrganize(onChanged);
  const sortedAfterSuggest = useRef(false);
  // Once every suggested workspace is added, sort the meetings into them.
  useEffect(() => {
    const ideas = organize.job?.kind === "suggest" && !organize.job.active ? (organize.job.suggestions ?? []) : [];
    const left = ideas.filter((i) => !workspaces.some((w) => w.name.toLowerCase() === i.name.toLowerCase()));
    if (ideas.length && !left.length && !sortedAfterSuggest.current) {
      sortedAfterSuggest.current = true;
      organize.start("sort");
    }
  }, [workspaces, organize.job]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    api
      .device()
      .then(setDevice)
      .catch(() => undefined);
  }, []);

  const pulling = !!ollama?.pull?.active || !!ollama?.install?.active;
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
  const micDone = !native || soundCheckPassed(native);
  const speechDone =
    !speech || !speech.local || speech.models.every((m) => m.installed);
  const usingOllama = health.summary.id === "ollama";
  const summaryDone = usingOllama
    ? !!ollama?.reachable && ollama.models.some((m) => GOOD_MODELS.test(m))
    : health.summary.ready;
  // A Mac short on memory or disk: a cloud AI for notes is the recommendation (done once one is chosen).
  const deviceDone = !device || device.verdict === "local" || !health.summary.local;
  // Open while suggestions are waiting to be added, or meetings are being sorted into new workspaces.
  const ideasLeft = (organize.job?.suggestions ?? []).filter(
    (idea) => !workspaces.some((w) => w.name.toLowerCase() === idea.name.toLowerCase()),
  ).length;
  const spacesDone = skippedSpaces || (workspaces.length > 0 && !ideasLeft && !organize.job?.active);
  if (nameDone && micDone && speechDone && summaryDone && hasMeetings && deviceDone && spacesDone)
    return null;

  const dismiss = () => {
    setDismissed(true);
    remember(DISMISSED_KEY);
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
  const installOllama = () => {
    setError(null);
    api
      .installOllama()
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
        {device && (
          <Step
            done={deviceDone}
            title={
              deviceDone
                ? `Your Mac: ${[device.chip, `${device.ram_gb} GB memory`, `${device.disk_free_gb} GB free`].filter(Boolean).join(" · ")}`
                : device.verdict === "cloud"
                  ? "Your Mac is short on space: use a cloud AI for notes"
                  : "A cloud AI is recommended for notes on this Mac"
            }
          >
            {device.notes.map((n) => (
              <p key={n} className="mb-1">
                {n}
              </p>
            ))}
            <div className="mt-2 flex flex-wrap gap-2">
              <button className="btn btn-sm btn-primary" onClick={() => onOpenSettings("providers")}>
                Add a cloud AI
              </button>
            </div>
          </Step>
        )}
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
            title={
              micDone
                ? "Sound check passed: both sides of your calls get recorded"
                : "Run a sound check"
            }
          >
            <SoundCheck native={native} />
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
              {ollama?.install?.active ? (
                <>
                  {ollama.install.status}
                  {ollama.install.progress != null
                    ? ` · ${Math.round(ollama.install.progress * 100)}%`
                    : "…"}
                  <div
                    className="mt-2 h-1.5 overflow-hidden rounded-full bg-forest/10"
                    aria-hidden="true"
                  >
                    <div
                      className={`h-full rounded-full bg-forest transition-[width] duration-700 ${ollama.install.progress == null ? "w-1/3 animate-pulse" : ""}`}
                      style={
                        ollama.install.progress == null
                          ? undefined
                          : {
                              width: `${Math.max(2, ollama.install.progress * 100)}%`,
                            }
                      }
                    />
                  </div>
                </>
              ) : (
                <>
                  Summaries run on this Mac with Ollama, a free app for local AI
                  models (about 200 MB), or in the cloud with an API key.
                  <div className="mt-2 flex flex-wrap gap-2">
                    {ollama?.can_install ? (
                      <button
                        className="btn btn-sm btn-soft"
                        onClick={installOllama}
                      >
                        {ollama.installed
                          ? "Open Ollama"
                          : "Install Ollama for me"}
                      </button>
                    ) : (
                      <a
                        className="btn btn-sm btn-soft"
                        href="https://ollama.com/download"
                      >
                        Get Ollama
                      </a>
                    )}
                    <button
                      className="btn btn-sm btn-ghost"
                      onClick={() => onOpenSettings()}
                    >
                      Use an API key instead
                    </button>
                  </div>
                  {(error || ollama?.install?.error) && (
                    <p className="mt-1.5 text-trail-deep">
                      {error || ollama?.install?.error}
                    </p>
                  )}
                </>
              )}
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
                      onClick={() => onOpenSettings()}
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
                  onClick={() => onOpenSettings()}
                >
                  Open Settings
                </button>
              </div>
            </>
          )}
        </Step>
        <Step
          done={spacesDone}
          title={
            workspaces.length
              ? `Workspaces: ${workspaces.map((w) => w.name).join(", ")}`
              : skippedSpaces
                ? "Workspaces: maybe later"
                : "Organize into workspaces (optional)"
          }
        >
          Keep the parts of your life apart, like your job, a committee and
          personal, and switch between them in the sidebar. Everything starts in
          Default; Trailmix sorts meetings in as their notes are written.
          {meetingCount >= 3 && (
            <> It can also suggest workspaces from the topics of the meetings you have.</>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {meetingCount >= 3 && (
              <button
                className="btn btn-sm btn-primary"
                disabled={!!organize.job?.active}
                onClick={() => organize.start("suggest")}
              >
                Suggest from my meetings
              </button>
            )}
            <button className="btn btn-sm btn-soft" onClick={() => onOpenSettings("workspaces")}>
              Add my own
            </button>
            <button
              className="btn btn-sm btn-ghost"
              onClick={() => {
                setSkippedSpaces(true);
                remember(WORKSPACES_SKIPPED_KEY);
              }}
            >
              Skip
            </button>
          </div>
          <div className="mt-2">
            <OrganizeProgress job={organize.job} />
            {organize.error && <span className="text-trail-deep">{organize.error}</span>}
          </div>
          <WorkspaceSuggestions job={organize.job} existing={workspaces} onAdded={onChanged} />
          {workspaces.length > 0 && !ideasLeft && !organize.job?.active && (
            <button className="btn btn-sm btn-soft mt-2" onClick={() => organize.start("sort")}>
              Sort my meetings into them
            </button>
          )}
        </Step>
        <Step done={hasMeetings} title="Record your first meeting">
          Start your call as usual (Zoom, Google Meet, Teams, FaceTime), then
          press <b className="text-ink">Start recording</b> below
          {native?.shortcut_record ? (
            <>
              , or <kbd className="kbd">{native.shortcut_record}</kbd> from any
              app
            </>
          ) : null}
          . Trailmix records you and everyone on the call; the transcript and
          notes appear after you stop.
          {native?.shortcut_mark && (
            <>
              {" "}
              Press <kbd className="kbd">{native.shortcut_mark}</kbd> to flag a
              moment worth remembering.
            </>
          )}
          {native && (
            <>
              <p className="mt-1.5">
                Trailmix lives in your menu bar (the trail icon at the top right
                of your screen), so you can close this window any time and it
                keeps listening for the shortcut.
              </p>
              <label className="mt-2 flex cursor-pointer items-center gap-2 text-ink">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-[var(--forest)]"
                  checked={!!native.open_at_login}
                  onChange={(e) =>
                    void api
                      .setOpenAtLogin(e.target.checked)
                      .catch((err) => setError((err as Error).message))
                  }
                />
                Start Trailmix when I log in
              </label>
            </>
          )}
        </Step>
      </ol>
    </section>
  );
}
