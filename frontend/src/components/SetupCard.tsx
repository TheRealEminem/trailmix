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
import { TRANSCRIBERS, transcriberFor } from "../transcribers";
import type { Transcriber } from "../transcribers";
import { CheckIcon, CloseIcon } from "./icons";
import RecordingLaws from "./RecordingLaws";
import SoundCheck, { soundCheckPassed } from "./SoundCheck";
import { OrganizeProgress, WorkspaceSuggestions, useOrganize } from "./Workspaces";

const DISMISSED_KEY = "trailmix.setupDismissed";
const WORKSPACES_SKIPPED_KEY = "trailmix.workspacesSkipped";
const TRANSCRIPTION_CHOSEN_KEY = "trailmix.transcriptionChosen";
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
  personal,
}: {
  done: boolean;
  title: string;
  children?: React.ReactNode;
  /** The title has your details in it (your name, workspace names): hidden from problem reports. */
  personal?: boolean;
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
          data-private={personal ? "your details" : undefined}
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
  // Where speech becomes text: this Mac, or one of TRANSCRIBERS (Google, OpenAI, Groq, your own server).
  const [engine, setEngine] = useState<"local" | "remote" | "gemini">("local");
  const [remote, setRemote] = useState({ url: "", model: "", live: "" });
  const [savedKeys, setSavedKeys] = useState({ gemini: false, transcribe: false, openai: false });
  const [locked, setLocked] = useState(false);
  const [legal, setLegal] = useState({ region: "", retention: 30 });
  const [engineChosen, setEngineChosen] = useState(() => remembered(TRANSCRIPTION_CHOSEN_KEY));
  const [picking, setPicking] = useState<Transcriber | null>(null);
  const [cloudKey, setCloudKey] = useState("");
  const [serverUrl, setServerUrl] = useState("");
  const [engineBusy, setEngineBusy] = useState(false);
  const [engineError, setEngineError] = useState<string | null>(null);
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
        .then((s) => {
          if (!alive) return;
          setName(s.your_name);
          setEngine(s.transcribe_engine);
          setRemote({ url: s.transcribe_url, model: s.transcribe_model, live: s.transcribe_live_model });
          setSavedKeys({
            gemini: !!s.secret_hints?.gemini_api_key,
            transcribe: !!s.secret_hints?.transcribe_api_key,
            openai: !!s.secret_hints?.openai_api_key,
          });
          setLocked(!!s.lockdown);
          setLegal({ region: s.legal_region, retention: s.audio_retention_days });
        })
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
  const legalDone = legal.region !== "";
  const micDone = !native || soundCheckPassed(native);
  const speechDone =
    !speech || !speech.local || speech.models.every((m) => m.installed);
  const usingOllama = health.summary.id === "ollama";
  const summaryDone = usingOllama
    ? !!ollama?.reachable && ollama.models.some((m) => GOOD_MODELS.test(m))
    : health.summary.ready;
  // A Mac short on memory or disk: a cloud AI for notes is the recommendation (done once one is chosen).
  const deviceDone = !device || device.verdict === "local" || !health.summary.local;
  // Settled once you've picked, or it was already set up (a cloud engine, or meetings recorded before this step existed).
  const engineDone =
    engineChosen || engine === "remote" || (engine === "gemini" && savedKeys.gemini) || meetingCount > 0;
  const chosenCloud = transcriberFor(engine, remote.url);
  // Open while suggestions are waiting to be added, or meetings are being sorted into new workspaces.
  const ideasLeft = (organize.job?.suggestions ?? []).filter(
    (idea) => !workspaces.some((w) => w.name.toLowerCase() === idea.name.toLowerCase()),
  ).length;
  const spacesDone = skippedSpaces || (workspaces.length > 0 && !ideasLeft && !organize.job?.active);
  if (nameDone && legalDone && micDone && speechDone && summaryDone && hasMeetings && deviceDone && spacesDone && engineDone)
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
  const chooseThisMac = async () => {
    setEngineError(null);
    setPicking(null);
    try {
      await api.putSettings({ transcribe_engine: "local" });
      setEngine("local");
      setEngineChosen(true);
      remember(TRANSCRIPTION_CHOSEN_KEY);
      onChanged();
    } catch (e) {
      setEngineError((e as Error).message);
    }
  };
  // A key is already saved for this one (your own server may not need any).
  const keySaved = (t: Transcriber) =>
    t.engine === "gemini" ? savedKeys.gemini : t.id === "server" || (savedKeys.transcribe && remote.url === t.url);
  const ready = (t: Transcriber) =>
    (keySaved(t) || !!cloudKey.trim()) && (t.id !== "server" || /^https?:\/\/\S/.test(serverUrl.trim()));
  // Saves the choice and the key (if one was typed), and proves it works; goes back to how it was if it doesn't.
  const chooseCloud = async (t: Transcriber) => {
    const key = cloudKey.trim();
    const url = t.id === "server" ? serverUrl.trim().replace(/\/+$/, "") : t.url;
    // A different address gets the key typed here or none, so one service's key is never sent to another.
    const newKey = t.engine === "remote" && url !== remote.url;
    const shareKey = t.id === "openai" && !!key && !savedKeys.openai; // also writes notes with OpenAI
    setEngineBusy(true);
    setEngineError(null);
    try {
      await api.putSettings(
        t.engine === "gemini"
          ? { transcribe_engine: "gemini", ...(key ? { gemini_api_key: key } : {}) }
          : {
              transcribe_engine: "remote",
              transcribe_url: url,
              transcribe_model: t.model,
              transcribe_live_model: t.live,
              ...(key || newKey ? { transcribe_api_key: key } : {}),
              ...(shareKey ? { openai_api_key: key } : {}),
            },
      );
      try {
        await api.transcriptionTest();
      } catch (e) {
        // Not working: back to how it was, and don't keep a key that doesn't work.
        await api.putSettings({
          transcribe_engine: engine === t.engine ? "local" : engine,
          transcribe_url: remote.url,
          transcribe_model: remote.model,
          transcribe_live_model: remote.live,
          ...(key && t.engine === "gemini" ? { gemini_api_key: "" } : {}),
          ...(key && t.engine === "remote" ? { transcribe_api_key: "" } : {}),
          ...(shareKey ? { openai_api_key: "" } : {}),
        });
        throw e;
      }
      setEngine(t.engine);
      if (t.engine === "remote") setRemote({ url, model: t.model, live: t.live });
      setSavedKeys((k) =>
        t.engine === "gemini"
          ? { ...k, gemini: true }
          : { ...k, transcribe: !!key || (k.transcribe && !newKey), openai: k.openai || shareKey },
      );
      setCloudKey("");
      setPicking(null);
      setEngineChosen(true);
      remember(TRANSCRIPTION_CHOSEN_KEY);
      onChanged();
    } catch (e) {
      setEngineError((e as Error).message);
    } finally {
      setEngineBusy(false);
    }
  };

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
          personal
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
        <Step
          done={legalDone}
          title={legalDone ? "Recording laws where you are: read" : "Recording laws where you are"}
        >
          Some places require everyone on a call to agree before it's recorded or transcribed. Here's a short
          summary for where you are. Following it is up to you.
          <div className="mt-2">
            <RecordingLaws
              region={legal.region}
              retention={legal.retention}
              onSaved={() => {
                api.getSettings().then((s) => setLegal({ region: s.legal_region, retention: s.audio_retention_days }));
                onChanged();
              }}
            />
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
          done={engineDone}
          title={
            engineDone
              ? chosenCloud
                ? `Transcription: ${chosenCloud.id === "server" ? "your own server" : chosenCloud.name}${chosenCloud.engine === "gemini" ? ", with speaker labels" : ""}`
                : "Transcription: on this Mac"
              : "Choose how recordings become text"
          }
        >
          On this Mac is private and free, but a download of about 1.7 GB; it labels you and the other side of a call.
          A cloud service needs no download, but your recordings are sent to it; you bring your own API key.
          {device?.verdict === "cloud" && " This Mac is short on space, so a cloud service is the easier fit."}
          <div className="mt-2 flex flex-wrap gap-2">
            <button className="btn btn-sm btn-soft" onClick={() => void chooseThisMac()}>
              On this Mac
            </button>
            {!locked && (
              <button
                className={`btn btn-sm ${device?.verdict === "cloud" ? "btn-primary" : "btn-soft"}`}
                onClick={() => setPicking(picking ? null : TRANSCRIBERS[0])}
                aria-expanded={!!picking}
                disabled={engineBusy}
              >
                Cloud or your own server…
              </button>
            )}
          </div>
          {locked && <p className="mt-1.5">Lockdown mode is on, so recordings stay on this Mac.</p>}
          {picking && !locked && (
            <div className="mt-3">
              <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Transcription service">
                {TRANSCRIBERS.map((t) => (
                  <button
                    key={t.id}
                    role="radio"
                    aria-checked={picking.id === t.id}
                    className={`btn btn-sm rounded-full px-3.5 ${picking.id === t.id ? "btn-primary" : "btn-soft"}`}
                    onClick={() => {
                      setPicking(t);
                      setCloudKey("");
                      setEngineError(null);
                    }}
                  >
                    {t.name}
                  </button>
                ))}
              </div>
              <p className="mt-2 mb-1.5">
                {picking.about}
                {picking.keyUrl && (
                  <>
                    {" "}
                    Create a key at{" "}
                    <a className="underline" href={picking.keyUrl} target="_blank" rel="noreferrer">
                      {picking.keyUrl.replace(/^https:\/\//, "")}
                    </a>
                    ; it's kept on this computer.
                  </>
                )}
                {picking.id === "server" && " The model and live draft can be changed in Settings → Transcription."}
              </p>
              <div className="flex flex-wrap gap-2">
                {picking.id === "server" && (
                  <input
                    className="field h-8 w-64 px-2.5 font-mono text-label"
                    placeholder="http://my-mac.local:8770/v1"
                    value={serverUrl}
                    onChange={(e) => setServerUrl(e.target.value)}
                    aria-label="Server address"
                    autoComplete="off"
                  />
                )}
                <input
                  type="password"
                  className="field h-8 w-64 px-2.5 text-label"
                  placeholder={
                    keySaved(picking)
                      ? picking.id === "server"
                        ? "Token (optional)"
                        : `Saved ${picking.name} key`
                      : `${picking.name} API key`
                  }
                  value={cloudKey}
                  onChange={(e) => setCloudKey(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && ready(picking) && void chooseCloud(picking)}
                  aria-label={`${picking.name} API key`}
                  autoComplete="off"
                />
                <button
                  className="btn btn-sm btn-primary"
                  onClick={() => void chooseCloud(picking)}
                  disabled={!ready(picking) || engineBusy}
                >
                  {engineBusy
                    ? "Checking…"
                    : keySaved(picking) && !cloudKey.trim() && picking.id !== "server"
                      ? `Use ${picking.name}`
                      : "Save and check"}
                </button>
              </div>
            </div>
          )}
          {engineError && <p className="mt-1.5 text-trail-deep">{engineError}</p>}
        </Step>
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
          personal
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
