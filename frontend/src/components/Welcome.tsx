import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { PRIVACY_POLICY, api } from "../api";
import type {
  DeviceAdvice,
  Health,
  NativeRecorder,
  OllamaStatus,
  ProviderId,
  RecordingLaws as LawTable,
  Settings,
  SpeechModels,
} from "../api";
import {
  ArrowRightIcon,
  ChartIcon,
  CheckIcon,
  ChevronLeftIcon,
  CloudIcon,
  CompassIcon,
  LaptopIcon,
  MicIcon,
  SparkleIcon,
  UsersIcon,
  WaveIcon,
} from "./icons";
import { Ambient, Logo, TrailScene } from "./illustrations";
import { CONSENT, Source, entryFor, guess, regionOptions, stamp } from "./RecordingLaws";
import SoundCheck, { soundCheckPassed } from "./SoundCheck";
import { Collapse, Segmented, Select, Spinner, Switch } from "./ui";

/** The home-screen checklist this replaced: closing it meant "I've done setup", so it still does. */
const OLD_CHECKLIST_CLOSED = "trailmix.setupDismissed";
// Instruction-following families that write good notes (see llm_engine.preferred_ollama_model).
const GOOD_MODELS = /^(qwen3|qwen2\.5|gemma3|llama3\.[12]|mistral|phi4)/i;

/** Should a new install be walked through setup? Not once it's been done, skipped, or meetings exist. */
export function needsSetup(s: Settings, meetingCount: number): boolean {
  let closedChecklist = false;
  try {
    closedChecklist = localStorage.getItem(OLD_CHECKLIST_CLOSED) === "1";
  } catch {
    /* private window */
  }
  return !s.setup_done && meetingCount === 0 && !closedChecklist;
}

type StepId = "hello" | "name" | "laws" | "transcribe" | "notes" | "sound" | "stats" | "done";

interface Props {
  health: Health | null;
  native: NativeRecorder | null;
  dusk: boolean;
  /** Something was saved: refresh what the rest of the app shows. */
  onChanged: () => void;
  onDone: () => void;
}

/**
 * First run, one question per screen: your name, where you are (recording laws), how recordings become text,
 * who writes the notes, a sound check, usage stats, and a last screen on how to record. Every screen can be
 * skipped, and all of it is in Settings later. Settings → App → Run setup again brings it back.
 */
export default function Welcome({ health, native, dusk, onChanged, onDone }: Props) {
  const [s, setS] = useState<Settings | null>(null);
  const [device, setDevice] = useState<DeviceAdvice | null>(null);
  const [askStats, setAskStats] = useState(false);
  const [at, setAt] = useState<StepId>("hello");
  // The menu bar recorder can drop out of a poll for a moment: once it's been seen, keep its step.
  const [hasNative, setHasNative] = useState(!!native);
  useEffect(() => {
    if (native) setHasNative(true);
  }, [native]);

  useEffect(() => {
    void Promise.allSettled([api.getSettings(), api.device(), api.stats()]).then(([settings, dev, stats]) => {
      if (dev.status === "fulfilled") setDevice(dev.value);
      if (settings.status === "fulfilled") {
        setS(settings.value);
        setAskStats(stats.status === "fulfilled" && stats.value.available && !settings.value.stats_notice_seen);
      }
    });
  }, []);

  // Decided once from how things were when setup opened, so the steps don't shift while you're in them.
  const [steps, setSteps] = useState<StepId[] | null>(null);
  useEffect(() => {
    if (!s || steps) return;
    setSteps([
      "hello",
      "name",
      "laws",
      ...(s.transcribe_engine === "remote" ? [] : (["transcribe"] as const)),
      "notes",
      ...(hasNative ? (["sound"] as const) : []),
      ...(askStats ? (["stats"] as const) : []),
      "done",
    ]);
  }, [s, steps, hasNative, askStats]);

  const saved = (next: Settings) => {
    setS(next);
    onChanged();
  };
  const finish = async () => {
    try {
      await api.putSettings({ setup_done: true });
    } catch {
      /* it asks again next time; nothing lost */
    }
    onChanged();
    onDone();
  };

  const index = steps ? steps.indexOf(at) : 0;
  const next = () => steps && setAt(steps[Math.min(index + 1, steps.length - 1)]);
  const back = () => steps && setAt(steps[Math.max(index - 1, 0)]);
  const nav = { onBack: back, next };

  let page: ReactNode = null;
  if (s && steps) {
    if (at === "hello") page = <Hello dusk={dusk} onStart={next} />;
    else if (at === "name") page = <NameStep s={s} onSaved={saved} {...nav} />;
    else if (at === "laws") page = <LawsStep s={s} onSaved={saved} {...nav} />;
    else if (at === "transcribe") page = <TranscribeStep s={s} device={device} onSaved={saved} {...nav} />;
    else if (at === "notes") page = <NotesStep s={s} device={device} onSaved={saved} {...nav} />;
    else if (at === "sound") page = <SoundStep native={native} {...nav} />;
    else if (at === "stats") page = <StatsStep s={s} onSaved={saved} {...nav} />;
    else page = <DoneStep s={s} health={health} native={native} onBack={back} onFinish={finish} />;
  }
  // The dots count the questions, not the welcome and the last screen.
  const questions = steps ? steps.slice(1, -1) : [];
  const q = questions.indexOf(at);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Set up Trailmix"
      className="fixed inset-0 z-[60] flex flex-col overflow-y-auto bg-canvas"
    >
      <Ambient />
      <header className="relative z-10 flex items-center justify-between gap-3 px-5 py-4 sm:px-8 sm:py-5">
        <div className="flex items-center gap-2.5">
          <Logo size={28} />
          <span className="font-display text-lead font-semibold tracking-[-0.02em]">Trailmix</span>
        </div>
        {at !== "done" && s && (
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => void finish()}>
            Skip setup
          </button>
        )}
      </header>

      <div className="relative z-10 flex flex-1 justify-center px-5 pb-8 pt-2 sm:items-center sm:pt-0">
        {page ? (
          <div key={at} className="w-full max-w-[560px] animate-enter">
            {page}
          </div>
        ) : (
          <div className="flex items-center animate-soft-pulse">
            <Logo size={48} />
          </div>
        )}
      </div>

      <footer className="relative z-10 flex h-14 shrink-0 items-center justify-center">
        {q >= 0 && (
          <div className="flex items-center gap-2" aria-label={`Step ${q + 1} of ${questions.length}`} role="img">
            {questions.map((id, i) => (
              <span
                key={id}
                className={`h-1.5 rounded-full transition-all duration-300 ease-out ${
                  i === q ? "w-6 bg-forest" : i < q ? "w-1.5 bg-forest/50" : "w-1.5 bg-ink/15"
                }`}
              />
            ))}
          </div>
        )}
      </footer>
    </div>
  );
}

// ── The frame every question shares ────────────────────────────────────

interface NavProps {
  onBack: () => void;
  next: () => void;
}

/**
 * A question: an icon, a title, one line under it, the answer, and Back / Next. It's a form, so Enter in a
 * field presses Next. `onNext` saves the answer; if it throws, the message shows and you stay.
 */
function Page({
  icon,
  title,
  sub,
  children,
  onBack,
  onNext,
  nextLabel = "Next",
  disabled = false,
  quiet = false,
  aside,
}: {
  icon: ReactNode;
  title: ReactNode;
  sub?: ReactNode;
  children?: ReactNode;
  onBack?: () => void;
  onNext: () => Promise<void> | void;
  nextLabel?: string;
  disabled?: boolean;
  /** Next skips the question: a softer button, so what the page asks you to do stands out. */
  quiet?: boolean;
  /** A line beside the buttons, e.g. the privacy policy. */
  aside?: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await onNext();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!busy && !disabled) void go();
      }}
    >
      <span className="well well-forest mb-5 h-11 w-11 rounded-md">{icon}</span>
      <h1 className="page-title">{title}</h1>
      {sub && <p className="hero-sub mt-2">{sub}</p>}
      {children && <div className="mt-7">{children}</div>}
      {error && (
        <p role="alert" className="mt-5 text-ui leading-relaxed text-trail-deep">
          {error}
        </p>
      )}
      <div className="mt-8 flex items-center justify-between gap-3">
        {onBack ? (
          <button type="button" className="btn btn-md btn-ghost -ml-2" onClick={onBack} disabled={busy}>
            <ChevronLeftIcon size={18} /> Back
          </button>
        ) : (
          <span />
        )}
        <div className="flex items-center gap-4">
          {aside && <span className="hidden text-hint text-ink-soft sm:inline">{aside}</span>}
          <button
            type="submit"
            className={`btn btn-lg min-w-[128px] ${quiet ? "btn-soft" : "btn-primary"}`}
            disabled={busy || disabled}
          >
            {busy ? <Spinner /> : null}
            {nextLabel}
            {!busy && <ArrowRightIcon size={18} />}
          </button>
        </div>
      </div>
      {aside && <p className="mt-3 text-right text-hint text-ink-soft sm:hidden">{aside}</p>}
    </form>
  );
}

/** One of a few big options; what it needs (a key, a download) opens under it once picked. */
function Choice({
  selected,
  onSelect,
  icon,
  title,
  badge,
  disabled,
  children,
  body,
}: {
  selected: boolean;
  onSelect: () => void;
  icon: ReactNode;
  title: string;
  badge?: string;
  disabled?: boolean;
  body: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div
      className={`overflow-hidden rounded-lg border transition-[border-color,background-color,box-shadow] duration-200 ${
        selected ? "border-forest/60 bg-forest/[.04] shadow-focus" : "border-line-strong bg-surface"
      } ${disabled ? "opacity-60" : selected ? "" : "hover:border-forest/35"}`}
    >
      <button
        type="button"
        role="radio"
        aria-checked={selected}
        disabled={disabled}
        onClick={onSelect}
        className="flex w-full items-start gap-3.5 p-4 text-left disabled:cursor-not-allowed"
      >
        <span className={`well h-10 w-10 rounded-md ${selected ? "well-forest" : ""}`}>{icon}</span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2 text-body font-semibold text-ink">
            {title}
            {badge && <span className="chip bg-forest-soft text-forest-deep">{badge}</span>}
          </span>
          <span className="mt-1 block text-ui leading-relaxed text-ink-soft">{body}</span>
        </span>
        <span
          aria-hidden="true"
          className={`mt-1 flex h-5 w-5 shrink-0 items-center justify-center rounded-full transition-colors ${
            selected ? "bg-forest text-on-accent" : "ring-1 ring-inset ring-line-strong"
          }`}
        >
          {selected && <CheckIcon size={12} strokeWidth={3} />}
        </span>
      </button>
      <Collapse open={selected && !!children}>
        <div className="border-t border-line px-4 py-4 text-ui leading-relaxed text-ink-soft">{children}</div>
      </Collapse>
    </div>
  );
}

function Bar({ progress }: { progress: number | null | undefined }) {
  return (
    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-forest/10" aria-hidden="true">
      <div
        className={`h-full rounded-full bg-forest transition-[width] duration-700 ${progress == null ? "w-1/3 animate-pulse" : ""}`}
        style={progress == null ? undefined : { width: `${Math.max(2, progress * 100)}%` }}
      />
    </div>
  );
}

const pct = (p: number | null | undefined) => (p == null ? "…" : ` · ${Math.round(p * 100)}%`);

// ── The screens ────────────────────────────────────────────────────────

function Hello({ dusk, onStart }: { dusk: boolean; onStart: () => void }) {
  return (
    <div className="text-center">
      <div className="art-frame h-40 sm:h-52">
        <TrailScene dusk={dusk} parallax walking className="h-full w-full" />
      </div>
      <h1 className="hero-title mt-9">Welcome to Trailmix</h1>
      <p className="hero-sub mx-auto mt-3">
        Let's get you ready for your first meeting. A few quick questions, about two minutes.
      </p>
      <button type="button" autoFocus className="btn btn-hero btn-primary mt-8" onClick={onStart}>
        Get started <ArrowRightIcon size={20} />
      </button>
      <p className="mt-4 text-hint text-ink-soft">You can change any of it later in Settings.</p>
    </div>
  );
}

function NameStep({ s, onSaved, onBack, next }: NavProps & { s: Settings; onSaved: (s: Settings) => void }) {
  const [name, setName] = useState(s.your_name);
  const v = name.trim();
  return (
    <Page
      icon={<UsersIcon size={20} />}
      title="What's your name?"
      sub="Your notes and action items use it instead of “You”."
      onBack={onBack}
      nextLabel={v ? "Next" : "Skip"}
      quiet={!v}
      onNext={async () => {
        if (v && v !== s.your_name) onSaved(await api.putSettings({ your_name: v }));
        next();
      }}
    >
      <input
        autoFocus
        className="field h-12 w-full px-4 text-body"
        placeholder="Your name"
        aria-label="Your name"
        autoComplete="name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        data-private="your details"
      />
    </Page>
  );
}

function LawsStep({ s, onSaved, onBack, next }: NavProps & { s: Settings; onSaved: (s: Settings) => void }) {
  const [table, setTable] = useState<LawTable | null>(null);
  const [failed, setFailed] = useState(false);
  const [region, setRegion] = useState(s.legal_region);
  const [shorter, setShorter] = useState(false);
  const [more, setMore] = useState(false);
  useEffect(() => {
    api
      .recordingLaws()
      .then((t) => {
        setTable(t);
        if (!s.legal_region) setRegion(guess(t));
      })
      .catch(() => setFailed(true));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const entry = table ? entryFor(table, region) : null;
  const { countries, states } = table ? regionOptions(table) : { countries: [], states: [] };
  const country = region.startsWith("US") ? "US" : region;
  const suggest = entry?.retention && s.audio_retention_days !== entry.retention.suggest ? entry.retention : null;

  return (
    <Page
      icon={<CompassIcon size={20} />}
      title="Where are you?"
      sub="Some places require everyone on a call to agree before it's recorded. Here's the short version for yours."
      onBack={onBack}
      nextLabel={entry ? "Next" : "Skip"}
      quiet={!entry}
      onNext={async () => {
        if (entry && table)
          onSaved(
            await api.putSettings({
              legal_region: region,
              legal_seen: stamp(table),
              ...(shorter && suggest ? { audio_retention_days: suggest.suggest } : {}),
            }),
          );
        next();
      }}
    >
      {failed && <p className="text-ui text-ink-soft">The summaries couldn't be loaded. They're in Settings → Privacy too.</p>}
      {!table && !failed && (
        <p className="flex items-center gap-2 text-ui text-ink-soft">
          <Spinner /> Loading…
        </p>
      )}
      {table && (
        <div className="space-y-4">
          <div className="flex flex-wrap gap-2">
            <Select
              label="Country"
              className="w-full sm:w-64"
              value={country}
              onChange={(v) => {
                setRegion(v);
                setShorter(false);
              }}
              options={[...(country ? [] : [{ value: "", label: "Choose your country" }]), ...countries]}
            />
            {country === "US" && (
              <Select
                label="State"
                className="w-full sm:w-56"
                value={region === "US" ? "" : region}
                onChange={(v) => {
                  setRegion(v);
                  setShorter(false);
                }}
                options={[...(region === "US" ? [{ value: "", label: "Choose your state" }] : []), ...states]}
              />
            )}
          </div>
          {entry && (
            <div className="panel space-y-2.5 p-4 animate-fade-in">
              <span className={`chip ${CONSENT[entry.consent].tone}`}>{CONSENT[entry.consent].label}</span>
              <p className="text-label leading-relaxed text-ink">{entry.summary}</p>
              <button
                type="button"
                className="text-hint font-medium text-forest underline decoration-forest/30 underline-offset-2"
                onClick={() => setMore(!more)}
                aria-expanded={more}
              >
                {more ? "Less" : "More about this"}
              </button>
              <Collapse open={more}>
                <div className="space-y-2 text-hint leading-relaxed text-ink-soft">
                  {entry.notes && <p>{entry.notes}</p>}
                  {entry.sources.length > 0 && (
                    <p className="text-meta">
                      Sources:{" "}
                      {entry.sources.map((src, i) => (
                        <span key={src}>
                          {i > 0 && "; "}
                          <Source text={src} />
                        </span>
                      ))}
                      . Reviewed {entry.reviewed}.
                    </p>
                  )}
                  <p className="text-meta italic">{table.disclaimer}</p>
                </div>
              </Collapse>
            </div>
          )}
          {entry && suggest && (
            <label className="flex cursor-pointer items-start gap-2.5 text-ui leading-relaxed text-ink-soft">
              <input
                type="checkbox"
                className="mt-[3px] h-4 w-4 accent-[var(--forest)]"
                checked={shorter}
                onChange={(e) => setShorter(e.target.checked)}
              />
              <span>
                <span className="font-medium text-ink">
                  {suggest.suggest === 0
                    ? "Delete each recording once it's transcribed"
                    : `Keep recordings for ${suggest.suggest} days`}
                </span>{" "}
                (the transcript and notes stay). {suggest.why}
              </span>
            </label>
          )}
          {entry && <p className="text-hint text-ink-soft">General information, not legal advice. Following it is up to you.</p>}
        </div>
      )}
    </Page>
  );
}

function TranscribeStep({
  s,
  device,
  onSaved,
  onBack,
  next,
}: NavProps & { s: Settings; device: DeviceAdvice | null; onSaved: (s: Settings) => void }) {
  const keySaved = !!s.secret_hints?.gemini_api_key;
  const [pick, setPick] = useState<"local" | "gemini">(s.transcribe_engine === "gemini" && !s.lockdown ? "gemini" : "local");
  const [key, setKey] = useState("");
  const cloudFits = device?.verdict === "cloud";

  const save = async () => {
    if (pick === "local") {
      if (s.transcribe_engine !== "local") onSaved(await api.putSettings({ transcribe_engine: "local" }));
      return next();
    }
    const typed = key.trim();
    await api.putSettings({ transcribe_engine: "gemini", ...(typed ? { gemini_api_key: typed } : {}) });
    try {
      await api.transcriptionTest();
    } catch (e) {
      // Not working: back to how it was, and don't keep a key that doesn't work.
      onSaved(
        await api.putSettings({
          transcribe_engine: s.transcribe_engine === "gemini" ? "local" : s.transcribe_engine,
          ...(typed ? { gemini_api_key: "" } : {}),
        }),
      );
      throw new Error(`Google didn't accept that: ${(e as Error).message}`);
    }
    onSaved(await api.getSettings());
    next();
  };

  return (
    <Page
      icon={<WaveIcon size={20} />}
      title="How should recordings become text?"
      sub="Pick where the speech-to-text happens."
      onBack={onBack}
      onNext={save}
      nextLabel={pick === "gemini" && (key.trim() || !keySaved) ? "Check and continue" : "Next"}
      disabled={pick === "gemini" && !keySaved && !key.trim()}
    >
      <div role="radiogroup" aria-label="Transcription" className="space-y-3">
        <Choice
          selected={pick === "local"}
          onSelect={() => setPick("local")}
          icon={<LaptopIcon size={18} />}
          title="On this Mac"
          badge={cloudFits ? undefined : "Recommended"}
          body="Private and free. A one-time download of about 1.7 GB, and it tells you apart from the other side."
        />
        <Choice
          selected={pick === "gemini"}
          onSelect={() => setPick("gemini")}
          disabled={s.lockdown}
          icon={<CloudIcon size={18} />}
          title="Google Gemini"
          badge={cloudFits && !s.lockdown ? "Recommended for this Mac" : undefined}
          body={
            s.lockdown
              ? "Lockdown mode is on, so recordings stay on this Mac."
              : "Tells each person on the call apart, with no download. Recordings are sent to Google."
          }
        >
          {keySaved ? (
            <p>Uses the Google API key you've already added.</p>
          ) : (
            <>
              <p className="mb-2.5">
                Paste a Google AI API key. A free one works; get it at{" "}
                <a className="text-forest underline" href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">
                  aistudio.google.com/apikey
                </a>
                . It stays on this computer.
              </p>
              <input
                type="password"
                className="field h-10 w-full px-3"
                placeholder="Google API key"
                aria-label="Google API key"
                autoComplete="off"
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
            </>
          )}
        </Choice>
      </div>
    </Page>
  );
}

const CLOUD: { id: Exclude<ProviderId, "ollama" | "custom">; label: string; keyUrl: string }[] = [
  { id: "anthropic", label: "Claude", keyUrl: "https://console.anthropic.com/settings/keys" },
  { id: "openai", label: "OpenAI", keyUrl: "https://platform.openai.com/api-keys" },
  { id: "gemini", label: "Gemini", keyUrl: "https://aistudio.google.com/apikey" },
  { id: "deepseek", label: "DeepSeek", keyUrl: "https://platform.deepseek.com/api_keys" },
];

function NotesStep({
  s,
  device,
  onSaved,
  onBack,
  next,
}: NavProps & { s: Settings; device: DeviceAdvice | null; onSaved: (s: Settings) => void }) {
  const current = CLOUD.find((c) => c.id === s.summary_provider);
  const [mode, setMode] = useState<"local" | "cloud">(
    s.lockdown ? "local" : current || s.summary_provider === "custom" || device?.verdict === "cloud" ? "cloud" : "local",
  );
  // A key you've added (for transcription, say) picks its provider.
  const [cloud, setCloud] = useState(
    current?.id ?? CLOUD.find((c) => s.secret_hints?.[`${c.id}_api_key`])?.id ?? "anthropic",
  );
  const [key, setKey] = useState("");
  const hint = s.secret_hints?.[`${cloud}_api_key`];
  const meta = CLOUD.find((c) => c.id === cloud)!;
  const customKept = s.summary_provider === "custom" && mode === "cloud";

  const save = async () => {
    if (mode === "local") {
      if (s.summary_provider !== "ollama") onSaved(await api.putSettings({ summary_provider: "ollama" }));
      return next();
    }
    if (customKept && !key.trim()) return next();
    const typed = key.trim();
    const keyField = `${cloud}_api_key`;
    await api.putSettings({ summary_provider: cloud, ...(typed ? { [keyField]: typed } : {}) } as Partial<Settings>);
    try {
      await api.providerTest(cloud);
    } catch (e) {
      onSaved(
        await api.putSettings({
          summary_provider: s.summary_provider,
          ...(typed ? { [keyField]: "" } : {}),
        } as Partial<Settings>),
      );
      throw new Error(`${meta.label} didn't accept that: ${(e as Error).message}`);
    }
    onSaved(await api.getSettings());
    next();
  };

  return (
    <Page
      icon={<SparkleIcon size={20} />}
      title="Who should write your notes?"
      sub="After each meeting, an AI writes the summary, the decisions and the action items."
      onBack={onBack}
      onNext={save}
      nextLabel={mode === "cloud" && key.trim() ? "Check and continue" : "Next"}
      disabled={mode === "cloud" && !customKept && !hint && !key.trim()}
    >
      {device && (
        <p className="-mt-3 mb-4 text-hint text-ink-soft">
          This Mac: {[device.chip, `${device.ram_gb} GB memory`, `${device.disk_free_gb} GB free`].filter(Boolean).join(" · ")}
        </p>
      )}
      <div role="radiogroup" aria-label="Notes" className="space-y-3">
        <Choice
          selected={mode === "local"}
          onSelect={() => setMode("local")}
          icon={<LaptopIcon size={18} />}
          title="On this Mac"
          badge={device?.verdict === "local" ? "Recommended" : undefined}
          body="Private and free, with Ollama, an app that runs AI models on your Mac."
        >
          {device && device.verdict !== "local" && (
            <p className="mb-3 rounded-sm bg-sun-soft px-3 py-2 text-sun-deep">{device.notes.join(" ")}</p>
          )}
          <OllamaSetup onReady={() => void api.getSettings().then(onSaved)} />
        </Choice>
        <Choice
          selected={mode === "cloud"}
          onSelect={() => setMode("cloud")}
          disabled={s.lockdown}
          icon={<CloudIcon size={18} />}
          title="A cloud AI"
          badge={device && device.verdict !== "local" && !s.lockdown ? "Recommended for this Mac" : undefined}
          body={
            s.lockdown
              ? "Lockdown mode is on, so everything stays on this Mac."
              : "The best notes, from Claude, OpenAI, Gemini or DeepSeek, with your own API key. Transcripts are sent to them."
          }
        >
          {customKept ? (
            <p>Uses your own OpenAI-compatible server ({s.custom_name}), set up in Settings → AI.</p>
          ) : (
            <>
              <Segmented
                label="Cloud AI"
                size="sm"
                value={cloud}
                onChange={(v) => {
                  setCloud(v);
                  setKey("");
                }}
                options={CLOUD.map((c) => ({ id: c.id, label: c.label }))}
              />
              <p className="mb-2.5 mt-3">
                {hint ? (
                  <>Uses the {meta.label} key you've added ({hint}). Paste another to replace it.</>
                ) : (
                  <>
                    Paste your {meta.label} API key; get one at{" "}
                    <a className="text-forest underline" href={meta.keyUrl} target="_blank" rel="noreferrer">
                      {new URL(meta.keyUrl).hostname}
                    </a>
                    . It stays on this computer.
                  </>
                )}
              </p>
              <input
                type="password"
                className="field h-10 w-full px-3"
                placeholder={`${meta.label} API key`}
                aria-label={`${meta.label} API key`}
                autoComplete="off"
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
            </>
          )}
        </Choice>
      </div>
      <p className="mt-4 text-hint text-ink-soft">A backup AI, templates and other providers are in Settings → AI.</p>
    </Page>
  );
}

/**
 * Getting Ollama and a good notes model onto this Mac, in place: install, download, ready. `onReady` when a
 * download or install finishes, since that changes what the app can do.
 */
function OllamaSetup({ onReady }: { onReady: () => void }) {
  const [o, setO] = useState<OllamaStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = !!o?.pull?.active || !!o?.install?.active;
  const wasBusy = useRef(false);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .ollama()
        .then((v) => alive && setO(v))
        .catch(() => undefined);
    load();
    const t = setInterval(load, busy ? 1500 : 4000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [busy]);
  useEffect(() => {
    if (busy) wasBusy.current = true;
    else if (wasBusy.current) {
      wasBusy.current = false;
      onReady();
    }
  }, [busy]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = (call: () => Promise<OllamaStatus>) => {
    setError(null);
    call()
      .then(setO)
      .catch((e) => setError((e as Error).message));
  };
  const failure = error || o?.install?.error || o?.pull?.error;
  const good = o?.models.find((m) => GOOD_MODELS.test(m));

  if (!o)
    return (
      <p className="flex items-center gap-2">
        <Spinner /> Looking for Ollama…
      </p>
    );
  return (
    <div>
      {!o.reachable && o.install?.active && (
        <>
          {o.install.status}
          {pct(o.install.progress)}
          <Bar progress={o.install.progress} />
        </>
      )}
      {!o.reachable && !o.install?.active && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          {o.can_install ? (
            <button type="button" className="btn btn-sm btn-primary" onClick={() => run(api.installOllama)}>
              {o.installed ? "Open Ollama" : "Install Ollama for me"}
            </button>
          ) : (
            <a className="btn btn-sm btn-primary" href="https://ollama.com/download" target="_blank" rel="noreferrer">
              Get Ollama
            </a>
          )}
          <span>{o.installed ? "Ollama is installed but not running." : "About 200 MB."}</span>
        </div>
      )}
      {o.reachable && o.pull?.active && (
        <>
          Downloading {o.pull.model}
          {pct(o.pull.progress)}
          <Bar progress={o.pull.progress} />
        </>
      )}
      {o.reachable && !o.pull?.active && good && (
        <p className="flex items-center gap-2 text-forest-deep">
          <CheckIcon size={16} /> Ready, with {o.using || good}.
        </p>
      )}
      {o.reachable && !o.pull?.active && !good && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <button type="button" className="btn btn-sm btn-primary" onClick={() => run(() => api.pullOllamaModel())}>
            Download {o.recommended}
          </button>
          <span>Writes good notes and suits this Mac.</span>
        </div>
      )}
      {busy && <p className="mt-2 text-hint">You can carry on while it downloads.</p>}
      {failure && <p className="mt-2 text-trail-deep">{failure}</p>}
    </div>
  );
}

function SoundStep({ native, onBack, next }: NavProps & { native: NativeRecorder | null }) {
  const passed = soundCheckPassed(native);
  return (
    <Page
      icon={<MicIcon size={20} />}
      title="Can Trailmix hear your calls?"
      sub="It plays two short chimes and listens for five seconds, to check it hears both you and the other side."
      onBack={onBack}
      onNext={next}
      nextLabel={passed ? "Next" : "Skip for now"}
      quiet={!passed}
    >
      {native ? (
        <SoundCheck native={native} brief />
      ) : (
        <p className="flex items-center gap-2 text-ui text-ink-soft">
          <Spinner /> Waiting for the menu bar recorder…
        </p>
      )}
    </Page>
  );
}

function StatsStep({ s, onSaved, onBack, next }: NavProps & { s: Settings; onSaved: (s: Settings) => void }) {
  const [share, setShare] = useState(s.share_stats && !s.lockdown);
  return (
    <Page
      icon={<ChartIcon size={20} />}
      title="Help Trailmix work on more computers"
      sub="Trailmix is free. A few anonymous numbers, once a day, show which Macs and set-ups to support."
      onBack={onBack}
      onNext={async () => {
        onSaved(await api.putSettings({ share_stats: share, stats_notice_seen: true }));
        next();
      }}
      aside={
        <>
          By continuing you agree to the{" "}
          <a className="text-forest underline decoration-forest/30 underline-offset-2" href={PRIVACY_POLICY} target="_blank" rel="noreferrer">
            privacy policy
          </a>
          .
        </>
      }
    >
      <div className="panel flex items-start gap-4 p-4">
        <div className="min-w-0 flex-1">
          <label htmlFor="share-stats" className="block cursor-pointer text-label font-medium text-ink">
            Share anonymous usage stats
          </label>
          <p className="mt-1 text-hint leading-relaxed text-ink-soft">
            The app version, your kind of computer, which features are on and how long processing takes.{" "}
            <b className="font-medium text-ink">Never anything from your meetings</b>, your name or your files.
          </p>
        </div>
        <Switch id="share-stats" checked={share} onChange={setShare} disabled={s.lockdown} label="Share anonymous usage stats" />
      </div>
    </Page>
  );
}

function DoneStep({
  s,
  health,
  native,
  onBack,
  onFinish,
}: {
  s: Settings;
  health: Health | null;
  native: NativeRecorder | null;
  onBack: () => void;
  onFinish: () => Promise<void>;
}) {
  const [speech, setSpeech] = useState<SpeechModels | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const downloading = !!speech?.models.some((m) => m.downloading);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .speechModels()
        .then((m) => alive && setSpeech(m))
        .catch(() => undefined);
    load();
    const t = setInterval(load, downloading ? 1500 : 6000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [downloading]);

  const first = s.your_name.trim().split(/\s+/)[0];
  const missing = speech?.local && s.transcribe_engine === "local" ? speech.models.filter((m) => !m.installed) : [];
  const current = missing.find((m) => m.downloading) ?? missing[0];
  const engine =
    s.transcribe_engine === "gemini" ? "Google Gemini" : s.transcribe_engine === "remote" ? "Your own server" : "On this Mac";
  const notes = health?.summary;

  return (
    <div>
      <span className="well well-forest mb-5 h-11 w-11 rounded-md">
        <CheckIcon size={22} strokeWidth={2.4} />
      </span>
      <h1 className="page-title">
        You're all set{first ? <span data-private="your details">, {first}</span> : null}.
      </h1>
      <p className="hero-sub mt-2">
        Start your call as usual (Zoom, Meet, Teams, FaceTime), then press <b className="font-medium text-ink">Start recording</b>
        {native?.shortcut_record ? (
          <>
            {" "}
            or <kbd className="kbd">{native.shortcut_record}</kbd> from any app
          </>
        ) : null}
        . The transcript and notes appear after you stop.
      </p>

      <ul className="panel mt-7 divide-y divide-line px-4">
        <Summary done label="Transcription" value={engine} />
        {current && (
          <Summary done={false} label="Speech models" value={`Downloading${pct(current.progress)}`}>
            <Bar progress={current.progress} />
            <p className="mt-1.5 text-hint text-ink-soft">You can record meanwhile; transcripts start when it's done.</p>
          </Summary>
        )}
        <Summary
          done={!!notes?.ready}
          label="Notes"
          value={notes ? (notes.ready ? `${notes.label}${notes.model ? ` · ${notes.model}` : ""}` : "Not ready yet") : "…"}
        >
          {notes && !notes.ready && <p className="text-hint text-ink-soft">{notes.reason || "Finish it in Settings → AI."}</p>}
        </Summary>
        {native && (
          <Summary
            done={soundCheckPassed(native)}
            label="Sound check"
            value={soundCheckPassed(native) ? "Both sides heard" : "Not yet: Test your audio, on the record screen"}
          />
        )}
      </ul>

      {native && (
        <div className="mt-5 space-y-3 text-ui leading-relaxed text-ink-soft">
          <p>
            Trailmix lives in your menu bar, so you can close this window and it keeps listening
            {native.shortcut_mark ? (
              <>
                . Press <kbd className="kbd">{native.shortcut_mark}</kbd> during a call to flag a moment
              </>
            ) : (
              " for the shortcut"
            )}
            .
          </p>
          {native.open_at_login !== undefined && native.open_at_login !== null && (
            <label className="flex cursor-pointer items-center gap-2.5 text-ink">
              <input
                type="checkbox"
                className="h-4 w-4 accent-[var(--forest)]"
                checked={native.open_at_login}
                onChange={(e) => void api.setOpenAtLogin(e.target.checked).catch((err) => setError((err as Error).message))}
              />
              Open Trailmix when I log in
            </label>
          )}
        </div>
      )}
      {error && <p className="mt-3 text-ui text-trail-deep">{error}</p>}

      <div className="mt-8 flex items-center justify-between gap-3">
        <button type="button" className="btn btn-md btn-ghost -ml-2" onClick={onBack} disabled={busy}>
          <ChevronLeftIcon size={18} /> Back
        </button>
        <button
          type="button"
          autoFocus
          className="btn btn-hero btn-primary"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            void onFinish();
          }}
        >
          {busy && <Spinner />} Start using Trailmix
        </button>
      </div>
    </div>
  );
}

function Summary({ done, label, value, children }: { done: boolean; label: string; value: string; children?: ReactNode }) {
  return (
    <li className="flex gap-3 py-3">
      <span
        className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${
          done ? "bg-forest text-on-accent" : "ring-1 ring-inset ring-line-strong"
        }`}
        aria-hidden="true"
      >
        {done && <CheckIcon size={12} strokeWidth={3} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
          <span className="text-label font-medium text-ink">{label}</span>
          <span className="text-ui text-ink-soft">{value}</span>
        </div>
        {children && <div className="mt-1">{children}</div>}
      </div>
    </li>
  );
}
