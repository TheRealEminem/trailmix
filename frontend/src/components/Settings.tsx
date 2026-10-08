import { useEffect, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { api, inApp } from "../api";
import type {
  ExportFormat,
  ExportJob,
  Health,
  NativeRecorder,
  ProviderId,
  Settings as SettingsT,
} from "../api";
import { tildePath } from "../format";
import type { ThemePref } from "../theme";
import {
  AlertIcon,
  BackpackIcon,
  CheckIcon,
  ChevronDownIcon,
  CloudIcon,
  ExportIcon,
  FolderIcon,
  KeyIcon,
  LaptopIcon,
  LinesIcon,
  MicIcon,
  MoonIcon,
  PencilIcon,
  DownloadIcon,
  PowerIcon,
  SparkleIcon,
  SunIcon,
} from "./icons";
import {
  Collapse,
  ControlRow,
  PageHeader,
  Segmented,
  Select,
  Skeleton,
  Spinner,
  SwitchRow,
} from "./ui";

interface Props {
  health: Health | null;
  native: NativeRecorder | null;
  theme: ThemePref;
  onTheme: (t: ThemePref) => void;
  onChanged: () => void;
  onQuit: () => void;
  onSignOut: () => void;
}

function Section({
  icon,
  title,
  blurb,
  delay = 0,
  id,
  children,
}: {
  icon: ReactNode;
  title: string;
  blurb: string;
  delay?: number;
  id?: string;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      className="mb-10 animate-enter scroll-mt-6"
      style={{ animationDelay: `${delay}ms` } as CSSProperties}
    >
      <div className="mb-3 flex items-start gap-3 px-1">
        <span className="well well-forest">{icon}</span>
        <div className="min-w-0">
          <h2 className="text-heading font-semibold tracking-[-0.01em]">
            {title}
          </h2>
          <p className="mt-0.5 text-ui leading-snug text-ink-soft">{blurb}</p>
        </div>
      </div>
      <div className="panel divide-y divide-line px-5 sm:px-6">{children}</div>
    </section>
  );
}

function Row({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <ControlRow label={label} hint={hint} wrap>
      {children}
    </ControlRow>
  );
}

/** A text field that saves when you leave it (or press Enter). */
function TextField({
  value,
  onSave,
  placeholder,
  mono,
  className = "w-full sm:w-64",
  disabled,
  list,
  label,
  id,
}: {
  value: string;
  onSave: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
  className?: string;
  disabled?: boolean;
  list?: string;
  label: string;
  id?: string;
}) {
  const [v, setV] = useState(value);
  useEffect(() => {
    setV(value);
  }, [value]);
  return (
    <input
      id={id}
      value={v}
      disabled={disabled}
      list={list}
      spellCheck={false}
      placeholder={placeholder}
      aria-label={label}
      onChange={(e) => setV(e.target.value)}
      onBlur={() => v !== value && onSave(v.trim())}
      onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
      className={`field ${mono ? "font-mono text-hint" : ""} ${className}`}
    />
  );
}

/** API keys are write-only: we show a hint of the saved one, never the key itself. */
function SecretField({
  hint,
  onSave,
  label,
}: {
  hint: string | null | undefined;
  onSave: (v: string) => void;
  label: string;
}) {
  const [v, setV] = useState("");
  return (
    <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:flex-nowrap">
      <input
        type="password"
        value={v}
        autoComplete="new-password"
        spellCheck={false}
        aria-label={label}
        placeholder={
          hint ? `Saved (${hint}), paste to replace` : "Paste API key"
        }
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && v.trim()) {
            onSave(v.trim());
            setV("");
          }
        }}
        className="field min-w-0 flex-1 font-mono text-hint sm:w-60 sm:flex-none"
      />
      <button
        className="btn btn-sm btn-soft"
        disabled={!v.trim()}
        onClick={() => {
          onSave(v.trim());
          setV("");
        }}
      >
        Save
      </button>
      {hint && !v && (
        <button
          className="btn btn-sm btn-danger-ghost"
          onClick={() => onSave("")}
        >
          Remove
        </button>
      )}
    </div>
  );
}

function TestResult({
  state,
}: {
  state: { ok: boolean; text: string } | "busy" | null;
}) {
  if (!state) return null;
  if (state === "busy")
    return (
      <p className="mt-2.5 flex items-center gap-2 text-hint text-ink-soft">
        <Spinner /> Checking…
      </p>
    );
  return (
    <p
      className={`mt-2.5 flex animate-fade-in items-start gap-1.5 text-hint leading-snug ${state.ok ? "text-forest-deep" : "text-trail-deep"}`}
    >
      {state.ok ? (
        <CheckIcon size={16} className="shrink-0" />
      ) : (
        <AlertIcon size={16} className="shrink-0" />
      )}
      <span className="break-words">{state.text}</span>
    </p>
  );
}

const PROVIDER_META: Record<
  ProviderId,
  { label: string; blurb: string; keyUrl?: string }
> = {
  ollama: {
    label: "Ollama",
    blurb:
      "Runs models on this Mac or any machine running Ollama. Nothing leaves your network.",
  },
  anthropic: {
    label: "Claude (Anthropic)",
    blurb: "Claude models via the Anthropic API.",
    keyUrl: "console.anthropic.com",
  },
  openai: {
    label: "OpenAI",
    blurb: "GPT models via the OpenAI API.",
    keyUrl: "platform.openai.com",
  },
  gemini: {
    label: "Google Gemini",
    blurb: "Gemini models via Google AI Studio.",
    keyUrl: "aistudio.google.com",
  },
  deepseek: {
    label: "DeepSeek",
    blurb: "DeepSeek models via the DeepSeek API.",
    keyUrl: "platform.deepseek.com",
  },
  custom: {
    label: "OpenAI-compatible",
    blurb:
      "Anything that speaks the OpenAI chat API: OpenRouter, Groq, LM Studio, vLLM…",
  },
};

/** A dropdown of the provider's models (a native menu, which works in the app window, unlike a suggestion
 *  list), plus "Other…" to type an id that isn't listed. */
/** Settings → Updates: which version this is, and checking for and installing a new one. */
function UpdatesSection({ native }: { native: NativeRecorder | null }) {
  const [error, setError] = useState<string | null>(null);
  const [asked, setAsked] = useState(false);
  const u = native?.update ?? null;
  const act = (fn: () => Promise<unknown>) => {
    setError(null);
    fn().catch((e) => setError((e as Error).message));
  };
  const checking = u?.state === "checking" || (asked && !u);
  const busy = u?.state === "downloading" || u?.state === "installing";
  const checked = u?.checked_at
    ? new Date(u.checked_at * 1000).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : null;

  let status: ReactNode;
  if (!u)
    status = native
      ? "This copy of Trailmix doesn't update itself (a development build)."
      : "Open Trailmix.app to check for updates.";
  else if (u.state === "available")
    status = (
      <span className="font-medium text-forest-deep">
        Trailmix {u.version} is available.
      </span>
    );
  else if (u.state === "downloading")
    status = `Downloading Trailmix ${u.version}${u.progress != null ? ` · ${Math.round(u.progress * 100)}%` : "…"}`;
  else if (u.state === "installing")
    status = `Installing Trailmix ${u.version}. It restarts in a moment…`;
  else if (u.state === "failed")
    status = (
      <span className="text-trail-deep">Couldn't update: {u.error}</span>
    );
  else if (u.state === "checking") status = "Checking for updates…";
  else if (u.state === "offline")
    status = "Couldn't reach GitHub to check. Are you online?";
  else if (u.state === "up-to-date") status = "You're up to date.";
  else status = "Trailmix checks for updates a few times a day.";

  return (
    <Section
      id="updates"
      icon={<DownloadIcon size={16} />}
      title="Updates"
      blurb="New versions install from here (or Help → Check for Updates…) in about a minute."
      delay={260}
    >
      <Row
        label={u?.current ? `Trailmix ${u.current}` : "Version"}
        hint={checked ? `Last checked ${checked}.` : undefined}
      >
        <span className="text-ui text-ink-soft">{status}</span>
      </Row>
      {u && (
        <div className="flex flex-wrap items-center gap-2 py-3.5">
          {u.state === "available" || u.state === "failed" ? (
            <button
              className="btn btn-sm btn-primary"
              onClick={() => act(api.installUpdate)}
            >
              <DownloadIcon size={16} />
              {u.state === "failed" ? "Try again" : `Update to ${u.version}`}
            </button>
          ) : (
            <button
              className="btn btn-sm btn-soft"
              disabled={busy || checking}
              onClick={() => {
                setAsked(true);
                act(api.checkForUpdate);
              }}
            >
              {checking ? <Spinner /> : null}
              Check for updates
            </button>
          )}
          {u.notes && (
            <a className="btn btn-sm btn-ghost" href={u.notes}>
              What's new
            </a>
          )}
          {u.state === "available" && (
            <span className="text-hint text-ink-soft">
              Trailmix restarts afterwards, and macOS asks for the microphone
              and system audio again.
            </span>
          )}
        </div>
      )}
      {error && <p className="pb-3.5 text-hint text-trail-deep">{error}</p>}
    </Section>
  );
}

function ModelPicker({
  label,
  value,
  models,
  local,
  onSave,
  onLoad,
}: {
  label: string;
  value: string;
  models: string[];
  local: boolean;
  onSave: (v: string) => void;
  onLoad: () => void;
}) {
  const listed = !value || models.includes(value);
  const [typing, setTyping] = useState(false);
  const options =
    value && !models.includes(value) ? [value, ...models] : models;
  return (
    <div className="flex w-full items-center gap-2 sm:w-auto">
      {typing || (!models.length && !local) ? (
        <TextField
          label={label}
          value={value}
          onSave={(v) => {
            onSave(v);
            setTyping(false);
          }}
          mono
          className="min-w-0 flex-1 sm:w-56 sm:flex-none"
          placeholder={local ? "automatic" : "model id"}
        />
      ) : (
        <select
          aria-label={label}
          value={value}
          onChange={(e) =>
            e.target.value === "\u0000other"
              ? setTyping(true)
              : onSave(e.target.value)
          }
          className="field min-w-0 flex-1 font-mono text-hint sm:w-56 sm:flex-none"
        >
          {(local || !value) && (
            <option value="">{local ? "Automatic" : "Pick a model"}</option>
          )}
          {options.map((m) => (
            <option key={m} value={m}>
              {m}
              {!listed && m === value ? " (not found)" : ""}
            </option>
          ))}
          <option value={"\u0000other"}>Other…</option>
        </select>
      )}
      <button className="btn btn-sm btn-ghost" onClick={onLoad}>
        {models.length ? "Refresh" : "Load models"}
      </button>
    </div>
  );
}

function ProviderCard({
  id,
  s,
  save,
  primary,
  fallback,
}: {
  id: ProviderId;
  s: SettingsT;
  save: (c: Partial<SettingsT>) => Promise<void>;
  primary: boolean;
  fallback: boolean;
}) {
  const [open, setOpen] = useState(primary);
  const [models, setModels] = useState<string[]>([]);
  const [result, setResult] = useState<
    { ok: boolean; text: string } | "busy" | null
  >(null);
  const meta = PROVIDER_META[id];
  const hint = s.secret_hints?.[`${id}_api_key`];
  const modelKey = `${id}_model` as keyof SettingsT;
  const label = id === "custom" ? s.custom_name || meta.label : meta.label;
  const local = id === "ollama";
  const ready = local || (id === "custom" ? !!s.custom_base_url : !!hint);

  useEffect(() => {
    if (!open || !ready || models.length) return;
    api
      .providerModels(id)
      .then((r) => setModels(r.models))
      .catch(() => {}); // "Load models" shows the reason
  }, [open, ready, id, models.length]);

  const run = async (fn: () => Promise<string>) => {
    setResult("busy");
    try {
      setResult({ ok: true, text: await fn() });
    } catch (e) {
      setResult({ ok: false, text: (e as Error).message });
    }
  };

  return (
    <div className="py-2">
      <button
        onClick={() => setOpen(!open)}
        className="-mx-2 flex w-[calc(100%+1rem)] items-center justify-between gap-3 rounded-md px-2 py-2 text-left transition-colors duration-150 hover:bg-forest/[.04]"
        aria-expanded={open}
      >
        <span className="flex min-w-0 items-center gap-3.5">
          <span
            className={`well ${local ? "well-forest" : "bg-sky-soft text-sky-deep"}`}
          >
            {local ? <LaptopIcon size={16} /> : <CloudIcon size={16} />}
          </span>
          <span className="min-w-0">
            <span className="block text-label font-medium">{label}</span>
            <span className="block truncate text-hint text-ink-soft">
              {local
                ? s.ollama_url
                : ready
                  ? String(s[modelKey] || "no model picked")
                  : "Not set up"}
            </span>
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          {primary && (
            <span className="chip bg-forest-soft text-forest-deep">
              Default
            </span>
          )}
          {fallback && (
            <span className="chip bg-sun-soft text-sun-deep">Fallback</span>
          )}
          <span
            className={`chip hidden sm:inline-flex ${local ? "bg-forest-soft/60 text-forest-deep" : "bg-sky-soft text-sky-deep"}`}
          >
            {local ? "Local" : "Cloud"}
          </span>
          <ChevronDownIcon
            size={16}
            className={`ml-1 text-ink-faint transition-transform duration-200 ${open ? "rotate-180" : ""}`}
          />
        </span>
      </button>

      <Collapse open={open}>
        <div className="mb-2 mt-2 rounded-lg bg-surface-subtle/60 px-4 ring-1 ring-inset ring-line sm:ml-[2.875rem]">
          <p className="pt-3.5 text-hint leading-relaxed text-ink-soft">
            {meta.blurb}
            {meta.keyUrl && (
              <>
                {" "}
                Get a key at{" "}
                <span className="font-mono text-ink">{meta.keyUrl}</span>.
              </>
            )}
          </p>
          <div className="divide-y divide-line">
            {id === "custom" && (
              <>
                <Row label="Name">
                  <TextField
                    label="Name"
                    value={s.custom_name}
                    onSave={(v) => save({ custom_name: v })}
                    className="w-full sm:w-56"
                  />
                </Row>
                <Row
                  label="Base URL"
                  hint="Up to and including /v1, e.g. https://openrouter.ai/api/v1"
                >
                  <TextField
                    label="Base URL"
                    value={s.custom_base_url}
                    onSave={(v) => save({ custom_base_url: v })}
                    mono
                    placeholder="https://…/v1"
                  />
                </Row>
              </>
            )}
            {local ? (
              <Row
                label="Ollama URL"
                hint="Another machine works too, e.g. your Mac over Tailscale."
              >
                <TextField
                  label="Ollama URL"
                  value={s.ollama_url}
                  onSave={(v) => save({ ollama_url: v })}
                  mono
                />
              </Row>
            ) : (
              <Row
                label="API key"
                hint={
                  id === "custom" ? "Optional for local servers." : undefined
                }
              >
                <SecretField
                  label={`${label} API key`}
                  hint={hint}
                  onSave={(v) =>
                    save({ [`${id}_api_key`]: v } as Partial<SettingsT>)
                  }
                />
              </Row>
            )}
            <Row
              label="Model"
              hint={local ? "Empty uses the first installed model." : undefined}
            >
              <ModelPicker
                label={`${label} model`}
                value={String(s[modelKey] ?? "")}
                models={models}
                local={local}
                onSave={(v) => save({ [modelKey]: v } as Partial<SettingsT>)}
                onLoad={() =>
                  void run(async () => {
                    const r = await api.providerModels(id);
                    setModels(r.models);
                    return `Found ${r.models.length} models.`;
                  })
                }
              />
            </Row>
            <div className="py-3.5">
              <button
                className="btn btn-sm btn-soft"
                onClick={() =>
                  void run(async () => (await api.providerTest(id)).message)
                }
              >
                Test connection
              </button>
              <TestResult state={result} />
            </div>
          </div>
        </div>
      </Collapse>
    </div>
  );
}

// The Mac worker's address depends on your network, so that preset leaves the URL for you to type.
const TRANSCRIBE_PRESETS = [
  {
    name: "Trailmix Mac worker",
    url: "",
    model: "large",
    live: "live",
    hint: "Your own Mac: run ./trailmix worker on it, then enter its address.",
  },
  {
    name: "OpenAI",
    url: "https://api.openai.com/v1",
    model: "whisper-1",
    live: "whisper-1",
    hint: "Cloud; billed per minute.",
  },
  {
    name: "Groq",
    url: "https://api.groq.com/openai/v1",
    model: "whisper-large-v3-turbo",
    live: "whisper-large-v3-turbo",
    hint: "Cloud; very fast.",
  },
];

const PROVIDER_IDS: ProviderId[] = [
  "ollama",
  "anthropic",
  "openai",
  "gemini",
  "deepseek",
  "custom",
];

export default function Settings({
  health,
  native,
  theme,
  onTheme,
  onChanged,
  onQuit,
  onSignOut,
}: Props) {
  const [s, setS] = useState<SettingsT | null>(null);
  const [dir, setDir] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [tResult, setTResult] = useState<
    { ok: boolean; text: string } | "busy" | null
  >(null);
  const [needsAddress, setNeedsAddress] = useState(false);

  useEffect(() => {
    api
      .getSettings()
      .then((v) => {
        setS(v);
        setDir(v.export_dir);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const save = async (changes: Partial<SettingsT>) => {
    if (!s) return;
    setError(null);
    setS({ ...s, ...changes }); // optimistic
    try {
      setS(await api.putSettings(changes));
      onChanged();
    } catch (e) {
      setError((e as Error).message);
      const fresh = await api.getSettings();
      setS(fresh);
      setDir(fresh.export_dir);
    }
  };

  const testTranscription = async () => {
    setTResult("busy");
    try {
      setTResult({ ok: true, text: (await api.transcriptionTest()).message });
    } catch (e) {
      setTResult({ ok: false, text: (e as Error).message });
    }
  };

  // Transcription changes are checked straight away, so a wrong address shows up here, not in your next meeting.
  const saveTranscription = async (changes: Partial<SettingsT>) => {
    await save(changes);
    const next = { ...s, ...changes };
    if (next.transcribe_engine === "local" || next.transcribe_url) {
      setNeedsAddress(false);
      void testTranscription();
    } else {
      setTResult(null);
    }
  };

  const header = (
    <PageHeader
      title="Settings"
      subtitle="Pack your preferences once; they apply to every meeting after you stop recording."
    />
  );

  if (!s) {
    return (
      <div>
        {header}
        {error ? (
          <p className="text-label text-trail-deep">{error}</p>
        ) : (
          <div className="space-y-10" aria-label="Loading settings">
            {[0, 1].map((i) => (
              <div key={i}>
                <Skeleton className="mb-3 h-8 w-64" />
                <Skeleton className="h-40 w-full rounded-xl" />
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}-${pad(now.getMinutes())}`;
  const docNames =
    s.export_separate_files && s.export_summary && s.export_transcript
      ? ["Weekly sync - Notes", "Weekly sync - Transcript"]
      : ["Weekly sync"];
  const exampleDocs =
    s.export_summary || s.export_transcript
      ? docNames.flatMap((n) => s.export_formats.map((f) => `${n}.${f}`))
      : [];
  const providerLabel = (id: ProviderId) =>
    id === "custom"
      ? s.custom_name || PROVIDER_META.custom.label
      : PROVIDER_META[id].label;
  const providerOptions = PROVIDER_IDS.map((p) => ({
    value: p,
    label: providerLabel(p),
    hint: p === "ollama" ? "Local" : "Cloud",
  }));
  const cloudInUse = [s.summary_provider, s.summary_fallback].some(
    (p) => p !== "ollama" && p !== "none",
  );

  return (
    <div>
      {header}

      {error && (
        <div className="sticky top-[4.25rem] z-10 mb-6 flex animate-enter items-start gap-2.5 rounded-lg border border-trail/20 bg-trail-soft p-3.5 text-label text-trail-deep shadow-md lg:top-3">
          <AlertIcon size={18} className="mt-0.5 shrink-0" />
          {error}
        </div>
      )}

      <Section
        icon={<SunIcon size={16} />}
        title="Appearance & you"
        blurb="How Trailmix looks, and what it calls you."
        delay={40}
      >
        <Row label="Theme" hint="Dusk is the same trail after sunset.">
          <Segmented
            label="Theme"
            value={theme}
            onChange={onTheme}
            options={[
              { id: "light", label: "Day", icon: <SunIcon size={16} /> },
              { id: "dusk", label: "Dusk", icon: <MoonIcon size={16} /> },
              {
                id: "system",
                label: "Match Mac",
                icon: <LaptopIcon size={16} />,
              },
            ]}
          />
        </Row>
        <Row
          label="Your name"
          hint="Used instead of “You” in transcripts, summaries and exports."
        >
          <TextField
            label="Your name"
            value={s.your_name}
            onSave={(v) => save({ your_name: v })}
            placeholder="You"
            className="w-full sm:w-48"
          />
        </Row>
      </Section>

      <Section
        icon={<SparkleIcon size={16} />}
        title="Automatic mode"
        blurb="What happens on its own once a meeting ends."
        delay={80}
      >
        <SwitchRow
          icon={<LinesIcon size={16} />}
          label="Auto-generate transcript"
          hint="Off: the meeting waits for you to press “Generate transcript”."
          checked={s.auto_transcribe}
          onChange={(v) => save({ auto_transcribe: v })}
        />
        <SwitchRow
          icon={<SparkleIcon size={16} />}
          label="Auto-generate summary"
          hint="Off: after the transcript, the meeting waits for you to press “Summarize”."
          checked={s.auto_summarize}
          onChange={(v) => save({ auto_summarize: v })}
        />
        <SwitchRow
          icon={<PencilIcon size={16} />}
          label="Auto-title meetings"
          hint="Names the meeting from its summary. Meetings you rename yourself are never retitled."
          checked={s.auto_title}
          onChange={(v) => save({ auto_title: v })}
        />
        <p className="py-3.5 text-hint text-ink-soft">
          Short on free memory, Trailmix carries on and lets macOS use swap. It
          only waits (and asks) when that would be dire: under 10% of the disk
          free, or macOS reporting critical memory pressure.
        </p>
      </Section>

      <Section
        icon={<LinesIcon size={16} />}
        title="Summaries & questions"
        blurb="Which AI writes your notes and answers your questions."
        delay={120}
      >
        <Row label="Default provider">
          <Select
            label="Default provider"
            value={s.summary_provider}
            onChange={(v) => save({ summary_provider: v as ProviderId })}
            className="w-full sm:w-56"
            options={providerOptions}
          />
        </Row>
        <Row
          label="Fallback"
          hint="Tried if the default fails (offline, out of credit, and so on)."
        >
          <Select
            label="Fallback provider"
            value={s.summary_fallback}
            onChange={(v) =>
              save({ summary_fallback: v as SettingsT["summary_fallback"] })
            }
            className="w-full sm:w-56"
            options={[{ value: "none", label: "None" }, ...providerOptions]}
          />
        </Row>
        <Row
          label="Default template"
          hint="You can also pick one per meeting when you summarize."
        >
          <Select
            label="Default template"
            value={s.summary_template}
            onChange={(v) => save({ summary_template: v })}
            className="w-full sm:w-56"
            options={(health?.templates ?? []).map((t) => ({
              value: t.id,
              label: t.name,
            }))}
          />
        </Row>
        <div className="py-3.5">
          <div className="text-label font-medium">Custom template</div>
          <div className="mt-0.5 text-hint leading-snug text-ink-soft">
            Your own instructions for the “Custom” template, such as the
            sections you want. An Action Items list is always added at the end.
          </div>
          <CustomTemplate
            value={s.custom_template}
            onSave={(v) => save({ custom_template: v })}
          />
        </div>
        {cloudInUse && (
          <p className="flex items-start gap-2 py-3.5 text-hint leading-relaxed text-sun-deep">
            <CloudIcon size={16} className="mt-px shrink-0" />
            With a cloud provider, the transcript is sent to that company to
            write the summary or answer a question. Recording and transcription
            are unaffected.
          </p>
        )}
      </Section>

      <Section
        icon={<KeyIcon size={16} />}
        title="AI providers"
        blurb="Connect the models you want to use. Keys are stored in Trailmix's database and never shown again."
        delay={160}
      >
        {PROVIDER_IDS.map((id) => (
          <ProviderCard
            key={id}
            id={id}
            s={s}
            save={save}
            primary={s.summary_provider === id}
            fallback={s.summary_fallback === id}
          />
        ))}
      </Section>

      <Section
        icon={<MicIcon size={16} />}
        title="Transcription"
        blurb="Where speech becomes text, for both the live draft and the final transcript."
        delay={200}
      >
        <Row
          label="Engine"
          hint={
            health && !health.transcription.local_available
              ? "Local MLX isn't available on this machine."
              : undefined
          }
        >
          <Segmented
            label="Transcription engine"
            value={s.transcribe_engine}
            onChange={(v) => void saveTranscription({ transcribe_engine: v })}
            options={[
              { id: "local", label: "This Mac (MLX)" },
              { id: "remote", label: "Remote endpoint" },
            ]}
          />
        </Row>
        <SwitchRow
          icon={<LinesIcon size={16} />}
          label="Final transcript while recording"
          hint={
            s.live_final
              ? s.transcribe_engine === "local"
                ? "The accurate model transcribes as you talk (it uses about 2 GB of memory during calls), so the transcript is ready when you stop and the summary starts straight away."
                : "The transcription model works as you talk, so the transcript is ready when you stop."
              : "Off: a quick, rougher draft while recording, and the accurate transcript is made after the meeting."
          }
          checked={s.live_final}
          onChange={(v) => save({ live_final: v })}
        />
        {s.transcribe_engine === "remote" && (
          <>
            <div className="py-3.5">
              <div className="mb-2.5 text-label font-medium">Quick setup</div>
              <div className="flex flex-wrap gap-2">
                {TRANSCRIBE_PRESETS.map((p) => (
                  <button
                    key={p.name}
                    className="btn btn-sm btn-soft rounded-full px-3.5"
                    data-tip={p.hint}
                    onClick={() => {
                      if (p.url)
                        return void saveTranscription({
                          transcribe_url: p.url,
                          transcribe_model: p.model,
                          transcribe_live_model: p.live,
                        });
                      void save({
                        transcribe_url: "",
                        transcribe_model: p.model,
                        transcribe_live_model: p.live,
                      });
                      setTResult(null);
                      setNeedsAddress(true);
                      setTimeout(() =>
                        document.getElementById("transcribe-url")?.focus(),
                      );
                    }}
                  >
                    {p.name}
                  </button>
                ))}
              </div>
            </div>
            <Row
              label="Endpoint URL"
              hint={
                needsAddress
                  ? "Now type your Mac's address, e.g. http://my-mac.tail1234.ts.net:8770/v1 (run ./trailmix worker on that Mac)."
                  : !s.transcribe_url
                    ? "Not set yet: recordings can't be transcribed until it is."
                    : "Any OpenAI-compatible /audio/transcriptions service."
              }
            >
              <TextField
                id="transcribe-url"
                label="Endpoint URL"
                value={s.transcribe_url}
                onSave={(v) => void saveTranscription({ transcribe_url: v })}
                mono
                placeholder="https://…/v1"
              />
            </Row>
            <Row label="API key or token">
              <SecretField
                label="Transcription API key"
                hint={s.secret_hints?.transcribe_api_key}
                onSave={(v) =>
                  void saveTranscription({ transcribe_api_key: v })
                }
              />
            </Row>
            <Row label="Model" hint="Used for the final transcript.">
              <TextField
                label="Transcription model"
                value={s.transcribe_model}
                onSave={(v) => void saveTranscription({ transcribe_model: v })}
                mono
                className="w-full sm:w-56"
              />
            </Row>
            {!s.live_final && (
              <Row
                label="Live draft model"
                hint="Smaller and faster is fine. Empty uses the model above."
              >
                <TextField
                  label="Live draft model"
                  value={s.transcribe_live_model}
                  onSave={(v) => save({ transcribe_live_model: v })}
                  mono
                  className="w-full sm:w-56"
                />
              </Row>
            )}
          </>
        )}
        <div className="py-3.5">
          <button
            className="btn btn-sm btn-soft"
            onClick={() => void testTranscription()}
          >
            Test transcription
          </button>
          <TestResult state={tResult} />
        </div>
      </Section>

      <Section
        icon={<BackpackIcon size={16} />}
        title="Export"
        blurb="A folder per meeting, in files you own: the notes, all the details, and the recording if you like."
        delay={240}
      >
        <SwitchRow
          icon={<ExportIcon size={16} />}
          label="Export finished meetings automatically"
          hint="Each meeting is saved to its folder when its notes are done, and kept up to date when you rename it or tick off tasks."
          checked={s.auto_export}
          onChange={(v) => save({ auto_export: v })}
        />
        <div className="py-3.5">
          <label
            htmlFor="export-dir"
            className="flex items-center gap-2 text-label font-medium"
          >
            <FolderIcon size={16} className="text-ink-soft" />
            Export folder
          </label>
          <input
            id="export-dir"
            value={dir}
            spellCheck={false}
            onChange={(e) => setDir(e.target.value)}
            onBlur={() =>
              dir !== s.export_dir && void save({ export_dir: dir })
            }
            onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
            className="field mt-2.5 w-full font-mono text-hint"
          />
          <p className="mt-1.5 text-hint text-ink-soft">
            Created if it doesn't exist. Use a full path or start with ~.
          </p>
        </div>
        <div className="py-3.5">
          <div className="text-label font-medium">Documents</div>
          <p className="mt-0.5 text-hint text-ink-soft">
            The notes and transcript, in any of these. meeting.json, with every
            detail, is always included so meetings can be imported back.
          </p>
          <div className="mt-2.5 flex flex-wrap gap-2">
            {EXPORT_FORMATS.map((f) => {
              const on = s.export_formats.includes(f.id);
              return (
                <button
                  key={f.id}
                  type="button"
                  aria-pressed={on}
                  onClick={() =>
                    save({
                      export_formats: on
                        ? s.export_formats.filter((x) => x !== f.id)
                        : [...s.export_formats, f.id],
                    })
                  }
                  className={`chip h-8 gap-1.5 px-3 text-label ${on ? "bg-forest text-white" : "bg-surface-subtle text-ink-soft ring-1 ring-line hover:text-ink"}`}
                >
                  {on && <CheckIcon size={14} />}
                  {f.label}
                </button>
              );
            })}
          </div>
        </div>
        <SwitchRow
          label="Include the notes"
          checked={s.export_summary}
          onChange={(v) => save({ export_summary: v })}
        />
        <SwitchRow
          label="Include the transcript"
          checked={s.export_transcript}
          onChange={(v) => save({ export_transcript: v })}
        />
        <SwitchRow
          label="Notes and transcript as separate documents"
          checked={s.export_separate_files}
          disabled={!s.export_summary || !s.export_transcript}
          onChange={(v) => save({ export_separate_files: v })}
        />
        <SwitchRow
          label="Include the recording"
          hint="Recording.ogg, plus each side on its own. On the same disk it's the same file as Trailmix's copy (a hard link), so it takes no extra space, and it stays when the 30-day clean-up removes Trailmix's own. Meetings set to Keep forever always include it."
          checked={s.export_audio}
          onChange={(v) => save({ export_audio: v })}
        />
        <div className="py-3.5">
          <div className="text-meta text-ink-soft">Each meeting's folder</div>
          <pre className="mt-1.5 overflow-x-auto rounded-sm bg-surface-subtle px-3 py-2 font-mono text-meta leading-relaxed text-ink-soft">
            {[
              `${tildePath(s.export_dir)}/${stamp} Weekly sync/`,
              ...exampleDocs.map((d) => `    ${d}`),
              "    meeting.json",
              ...(s.export_audio
                ? ["    Recording.ogg", "    Tracks/You.ogg, Them.ogg"]
                : []),
            ].join("\n")}
          </pre>
        </div>
        <ExportAllRow includeAudio={s.export_audio} />
      </Section>

      <Section
        icon={<DownloadIcon size={16} />}
        title="Import"
        blurb="Bring in meetings recorded with other apps (see Import in the sidebar)."
        delay={260}
      >
        <Row
          label="Granola API key"
          hint="From Granola → Settings → Connectors → API keys (Business or Enterprise plan)."
        >
          <SecretField
            label="Granola API key"
            hint={s.secret_hints?.granola_api_key}
            onSave={(v) => save({ granola_api_key: v } as Partial<SettingsT>)}
          />
        </Row>
      </Section>

      <UpdatesSection native={native} />

      <Section
        icon={<PowerIcon size={16} />}
        title="App"
        blurb="Trailmix keeps running in the background until you quit it."
        delay={280}
      >
        {health?.auth ? (
          <Row
            label="Sign out"
            hint="You'll need the access token to get back in."
          >
            <button className="btn btn-sm btn-soft" onClick={onSignOut}>
              Sign out
            </button>
          </Row>
        ) : (
          <Row
            label="Quit Trailmix"
            hint={
              inApp
                ? "Closes Trailmix, including the menu bar recorder."
                : "Stops the background server. Open Trailmix.app to start it again."
            }
          >
            <button
              className="btn btn-sm btn-soft hover:!text-trail-deep"
              onClick={onQuit}
            >
              <PowerIcon size={16} /> Quit
            </button>
          </Row>
        )}
      </Section>
    </div>
  );
}

function CustomTemplate({
  value,
  onSave,
}: {
  value: string;
  onSave: (v: string) => void;
}) {
  const [v, setV] = useState(value);
  useEffect(() => {
    setV(value);
  }, [value]);
  return (
    <textarea
      value={v}
      onChange={(e) => setV(e.target.value)}
      onBlur={() => v !== value && onSave(v)}
      rows={4}
      aria-label="Custom template"
      placeholder={
        "Write the notes in Markdown with these sections:\n## Context\n## What we learned\n## Risks"
      }
      className="field mt-2.5 w-full resize-y font-mono text-hint leading-relaxed"
    />
  );
}

const EXPORT_FORMATS: { id: ExportFormat; label: string }[] = [
  { id: "pdf", label: "PDF" },
  { id: "docx", label: "Word (.docx)" },
  { id: "odt", label: "OpenDocument (.odt)" },
  { id: "md", label: "Markdown" },
  { id: "txt", label: "Plain text" },
];

/** "Export all meetings now": every transcribed meeting to its folder, with progress. */
function ExportAllRow({ includeAudio }: { includeAudio: boolean }) {
  const [job, setJob] = useState<ExportJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api
      .exportAllStatus()
      .then((j) => j.active && setJob(j))
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!job?.active) return;
    const t = setInterval(
      () =>
        api
          .exportAllStatus()
          .then(setJob)
          .catch(() => undefined),
      800,
    );
    return () => clearInterval(t);
  }, [job?.active]);
  const start = () => {
    setError(null);
    api
      .exportAll(includeAudio)
      .then(setJob)
      .catch((e) => setError((e as Error).message));
  };
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 py-3.5">
      <div className="min-w-0">
        <div className="text-label font-medium">Export all meetings now</div>
        <div className="mt-0.5 text-hint text-ink-soft">
          {job?.active
            ? `Exporting ${job.done} of ${job.total}…`
            : job && (job.total ?? 0) > 0
              ? `Exported ${job.done} meeting${job.done === 1 ? "" : "s"}${job.errors?.length ? `, ${job.errors.length} failed: ${job.errors[0]}` : "."}`
              : "Every meeting so far, with the settings above. Handy before reinstalling or moving to a new Mac."}
        </div>
        {error && <div className="mt-1 text-hint text-trail-deep">{error}</div>}
      </div>
      <button
        className="btn btn-sm btn-soft"
        onClick={start}
        disabled={!!job?.active}
      >
        {job?.active ? <Spinner /> : <ExportIcon size={16} />}
        Export all
      </button>
    </div>
  );
}
