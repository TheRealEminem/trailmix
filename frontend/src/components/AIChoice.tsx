import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import type { Health, Preflight, ProviderId, TranscribeEngine } from "../api";
import { shortModel } from "../format";
import { useBigSpend } from "../spend";
import { AlertIcon, ChevronDownIcon, CloudIcon, LaptopIcon, LinesIcon, SparkleIcon } from "./icons";
import { Segmented, Select, Spinner, Switch, TemplateSelect } from "./ui";

// Models a provider lists that can't write notes (speech, images, embeddings…).
const NOT_TEXT = /embed|tts|whisper|dall-e|imagen|image|audio|realtime|moderation|transcribe|veo|live|aqa|search/i;

/** A provider's models, loaded when it's picked; the current one is always offered. */
function ModelSelect({
  provider,
  value,
  onChange,
  className = "w-full",
}: {
  provider: ProviderId;
  value: string;
  onChange: (m: string) => void;
  className?: string;
}) {
  const [models, setModels] = useState<string[]>([]);
  useEffect(() => {
    let alive = true;
    setModels([]);
    api
      .providerModels(provider)
      .then((r) => alive && setModels(r.models.filter((m) => provider === "ollama" || !NOT_TEXT.test(m))))
      .catch(() => undefined); // offline or no key: just the current model
    return () => {
      alive = false;
    };
  }, [provider]);
  const all = [...new Set([value, ...models].filter(Boolean))];
  return (
    <Select
      label="Model"
      size="sm"
      value={value}
      onChange={onChange}
      className={className}
      options={all.length ? all.map((m) => ({ value: m, label: m })) : [{ value: "", label: "No models found" }]}
    />
  );
}

function money(c: number | null) {
  if (c === null) return null;
  if (c === 0) return "free";
  if (c < 0.01) return "under 1¢";
  return `about $${c < 0.1 ? c.toFixed(3) : c.toFixed(2)}`;
}

const gb = (n: number) => `${n.toFixed(1)} GB`;

const where = (local: boolean) => (local ? "here" : "cloud");

/** A line in the plan: an icon, what happens, and an optional warning tone. */
function PlanLine({ tone = "plain", children }: { tone?: "plain" | "warn"; children: ReactNode }) {
  return (
    <li className={`flex items-start gap-2 text-hint leading-relaxed ${tone === "warn" ? "text-sun-deep" : "text-ink-soft"}`}>
      {tone === "warn" ? (
        <AlertIcon size={14} className="mt-[3px] shrink-0" />
      ) : (
        <span aria-hidden="true" className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-ink-faint" />
      )}
      <span>{children}</span>
    </li>
  );
}

/**
 * Before anything runs: which AI transcribes these meetings (if they need it) and writes their notes, with what each
 * choice would take (passes over the transcript, memory on this Mac, money in the cloud).
 */
export function PreflightSheet({
  ids,
  health,
  onClose,
  onStarted,
}: {
  ids: number[];
  health: Health | null;
  onClose: () => void;
  onStarted: (queued: number) => void;
}) {
  const big = useBigSpend();
  const [data, setData] = useState<Preflight | null>(null);
  const [engine, setEngine] = useState<TranscribeEngine | null>(null);
  const [transcribeModel, setTranscribeModel] = useState("");
  const [provider, setProvider] = useState<ProviderId | null>(null);
  const [model, setModel] = useState("");
  const [template, setTemplate] = useState("");
  const [remember, setRemember] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const startRef = useRef<HTMLButtonElement>(null);
  const key = ids.join(",");

  // The plan for what's picked (the defaults to begin with), again whenever the pick changes.
  useEffect(() => {
    let alive = true;
    const t = setTimeout(
      () =>
        api
          .preflight({
            meeting_ids: ids,
            engine: engine ?? undefined,
            transcribe_model: transcribeModel || undefined,
            provider: provider ?? undefined,
            model: model || undefined,
          })
          .then((p) => {
            if (!alive) return;
            setData(p);
            if (provider === null) {
              setProvider(p.defaults.provider);
              setModel(p.notes.plan.model);
              setEngine(p.defaults.engine);
              setTranscribeModel(p.transcribe?.plan.model ?? "");
              setTemplate("");
            }
          })
          .catch((e: Error) => alive && setError(e.message)),
      data ? 200 : 0,
    );
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [key, engine, transcribeModel, provider, model]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !document.querySelector('[role="listbox"]') && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    if (data) startRef.current?.focus();
  }, [data !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  const notes = data?.notes;
  const plan = notes?.plan;
  const pickedOption = notes?.options.find((o) => o.provider === provider);
  const tx = data?.transcribe;
  const txOption = tx?.options.find((o) => o.engine === engine);
  const problem = (pickedOption && !pickedOption.ready && pickedOption.reason) || (txOption && !txOption.ready && txOption.reason) || null;
  const single = data?.meetings.length === 1 ? data.meetings[0] : null;

  // "On this Mac" / "In the cloud": the default provider of that kind if it's one, else the first that's set up.
  const pickKind = (local: boolean) => {
    if (!notes || pickedOption?.local === local) return;
    const fits = notes.options.filter((o) => o.local === local);
    const best =
      fits.find((o) => o.provider === data?.defaults.provider && o.ready) ?? fits.find((o) => o.ready) ?? fits[0];
    if (best) {
      setProvider(best.provider);
      setModel(best.model);
    }
  };

  const start = async () => {
    if (!provider) return;
    setStarting(true);
    setError(null);
    try {
      const r = await api.process({
        meeting_ids: ids,
        engine: tx ? (engine ?? undefined) : undefined,
        transcribe_model: tx ? transcribeModel || undefined : undefined,
        provider,
        model: model || undefined,
        template: template || undefined,
        remember,
      });
      onStarted(r.queued);
    } catch (e) {
      setError((e as Error).message);
      setStarting(false);
    }
  };

  const maxParts = plan ? Math.max(...plan.meetings.map((m) => m.parts)) : 1;
  const split = plan ? plan.meetings.filter((m) => m.parts > 1).length : 0;
  const short = plan && plan.ram_needed_gb !== null && plan.ram_needed_gb > plan.ram_free_gb;

  const dialog = (
    <div
      className="fixed inset-0 z-50 flex animate-fade-in items-center justify-center overflow-y-auto bg-[var(--scrim)] p-4 backdrop-blur-[3px]"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="preflight-title"
        className="my-auto w-full max-w-[560px] animate-dialog-in rounded-xl border border-line bg-surface shadow-lg"
      >
        <div className="px-6 pb-2 pt-6">
          <div className="eyebrow">Before it runs</div>
          <h2 id="preflight-title" className="mt-1 text-lead font-semibold tracking-[-0.01em]">
            {single ? `Process “${single.title}”` : data ? `Process ${data.meetings.length} meetings` : "Process"}
          </h2>
          <p className="mt-1 text-label leading-relaxed text-ink-soft">
            Nothing has run yet. Pick where the AI runs for {single ? "this one" : "these"}; the defaults live in
            Settings.
          </p>
        </div>

        {!data ? (
          <div className="flex items-center gap-2 px-6 py-10 text-label text-ink-soft">
            {error ? (
              <span className="text-trail-deep">{error}</span>
            ) : (
              <>
                <Spinner className="h-4 w-4" /> Working out what each choice would take…
              </>
            )}
          </div>
        ) : (
          <div className="space-y-5 px-6 py-4">
            {tx && engine && (
              <section aria-labelledby="pf-tx">
                <div className="mb-2 flex items-center gap-2">
                  <span className="well h-7 w-7">
                    <LinesIcon size={15} />
                  </span>
                  <h3 id="pf-tx" className="text-label font-semibold">
                    Transcript
                  </h3>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Segmented
                    size="sm"
                    label="Transcribe with"
                    value={engine}
                    onChange={(e) => {
                      setEngine(e);
                      setTranscribeModel(tx.options.find((o) => o.engine === e)?.models[0]?.id ?? "");
                    }}
                    options={tx.options.map((o) => ({
                      id: o.engine,
                      label: o.label,
                      icon: o.local ? <LaptopIcon size={14} /> : <CloudIcon size={14} />,
                    }))}
                  />
                  {txOption && txOption.models.length > 1 && (
                    <Select
                      label="Speech model"
                      size="sm"
                      value={transcribeModel}
                      onChange={setTranscribeModel}
                      className="w-56"
                      options={txOption.models.map((m) => ({ value: m.id, label: m.label, hint: m.id }))}
                    />
                  )}
                </div>
                <ul className="mt-2.5 space-y-1">
                  {engine === "local" ? (
                    <>
                      <PlanLine>
                        Free, on this Mac: {tx.plan.minutes} min of audio with {shortModel(tx.plan.model)}.
                      </PlanLine>
                      <PlanLine tone={(tx.plan.ram_needed_gb ?? 0) > tx.plan.ram_free_gb ? "warn" : "plain"}>
                        Needs about {gb(tx.plan.ram_needed_gb ?? 0)} of memory; {gb(tx.plan.ram_free_gb)} is free now.
                      </PlanLine>
                    </>
                  ) : (
                    <PlanLine tone={big(tx.plan.cost, tx.plan.minutes > 120) ? "warn" : "plain"}>
                      {tx.plan.minutes} min of audio sent to {txOption?.label ?? "the service"}
                      {big(tx.plan.cost, tx.plan.minutes > 120)
                        ? `: ${money(tx.plan.cost) ?? "a price Trailmix doesn't know"}${tx.plan.cost ? ` (prices as of ${data.as_of})` : ""}`
                        : ""}
                      .
                    </PlanLine>
                  )}
                </ul>
              </section>
            )}

            {notes && plan && provider && (
              <section aria-labelledby="pf-notes">
                <div className="mb-2 flex items-center gap-2">
                  <span className="well h-7 w-7">
                    <SparkleIcon size={15} />
                  </span>
                  <h3 id="pf-notes" className="text-label font-semibold">
                    Notes
                  </h3>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Segmented
                    size="sm"
                    label="Write notes"
                    value={where(!!pickedOption?.local)}
                    onChange={(v) => pickKind(v === "here")}
                    options={[
                      { id: "here", label: "On this Mac", icon: <LaptopIcon size={14} /> },
                      { id: "cloud", label: "In the cloud", icon: <CloudIcon size={14} /> },
                    ]}
                  />
                </div>
                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  <Select
                    label="Notes AI"
                    size="sm"
                    value={provider}
                    onChange={(v) => {
                      const o = notes.options.find((x) => x.provider === v);
                      setProvider(v as ProviderId);
                      setModel(o?.model ?? "");
                    }}
                    className="w-full"
                    options={notes.options
                      .filter((o) => o.local === !!pickedOption?.local)
                      .map((o) => ({
                        value: o.provider,
                        label: o.label,
                        hint: o.ready ? (o.local ? "On this Mac" : "Cloud") : "Not set up yet",
                      }))}
                  />
                  <ModelSelect provider={provider} value={model} onChange={setModel} />
                </div>
                <ul className="mt-2.5 space-y-1">
                  {maxParts > 1 ? (
                    <PlanLine tone={plan.local ? "warn" : "plain"}>
                      {single
                        ? `Too long for ${shortModel(plan.model)} to read at once, so it reads the transcript in ${maxParts} parts, then writes them up`
                        : `${split} of these ${split === 1 ? "is" : "are"} too long for ${shortModel(plan.model)} to read at once: read in parts, then written up`}
                      : about {plan.calls} requests in all{plan.local ? ", one after another on this Mac" : ""}.
                    </PlanLine>
                  ) : (
                    <PlanLine>
                      Reads {single ? "the whole transcript" : "each transcript"} in one go, then a few short requests
                      for a title and tags ({plan.calls} in all).
                    </PlanLine>
                  )}
                  {plan.local ? (
                    <>
                      <PlanLine tone={short ? "warn" : "plain"}>
                        Free. Needs about {gb(plan.ram_needed_gb ?? 0)} of memory; {gb(plan.ram_free_gb)} is free now
                        {short ? ", so macOS will swap and your Mac may feel slow until it's done" : ""}.
                      </PlanLine>
                      {data.recording && <PlanLine tone="warn">A recording is running: this waits until it ends.</PlanLine>}
                    </>
                  ) : (
                    <PlanLine tone={big(plan.cost, plan.tokens_in > 500_000) ? "warn" : "plain"}>
                      About {Math.round(plan.tokens_in / 1000)}k tokens sent to {plan.label}
                      {plan.runs_here ? " on this computer" : ""}
                      {big(plan.cost, plan.tokens_in > 500_000)
                        ? `: ${money(plan.cost) ?? `${plan.label} bills it; Trailmix doesn't know its price`}${plan.cost ? ` (prices as of ${data.as_of})` : ""}`
                        : ""}
                      .
                    </PlanLine>
                  )}
                </ul>
                <div className="mt-3 flex items-center gap-2">
                  <span className="text-hint text-ink-soft">Note style</span>
                  <TemplateSelect value={template} onChange={setTemplate} health={health} />
                </div>
              </section>
            )}

            {(problem || error) && (
              <div className="flex items-start gap-2 rounded-md bg-trail-soft/70 px-3 py-2.5 text-hint text-trail-deep">
                <AlertIcon size={15} className="mt-0.5 shrink-0" />
                <span>{error ?? problem}</span>
              </div>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-6 py-4">
          <label className="flex cursor-pointer items-center gap-2.5 text-hint text-ink-soft">
            <Switch checked={remember} onChange={setRemember} label="Make this my default" />
            Make this my default
          </label>
          <div className="flex gap-2">
            <button className="btn btn-md btn-ghost" onClick={onClose}>
              Not now
            </button>
            <button
              ref={startRef}
              className="btn btn-md btn-primary"
              disabled={!data || !!problem || starting}
              onClick={() => void start()}
            >
              {starting && <Spinner className="h-4 w-4" />}
              Start
            </button>
          </div>
        </div>
      </div>
    </div>
  );
  return createPortal(dialog, document.body);
}

/**
 * The quick switch for the defaults: where transcripts are made and which AI writes notes (and its model), and
 * whether imports wait for you to choose. Changes are saved as the defaults straight away.
 */
export function AISwitch({
  health,
  onChanged,
  onOpenSettings,
}: {
  health: Health | null;
  onChanged: () => void;
  onOpenSettings: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Element;
      if (!box.current?.contains(t) && !t.closest?.('[role="listbox"]')) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !document.querySelector('[role="listbox"]') && setOpen(false);
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!health) return null;
  const notes = health.summary;
  const providers = health.providers;
  const tx = health.transcription;

  const save = (changes: Parameters<typeof api.putSettings>[0]) => {
    setError(null);
    api
      .putSettings(changes)
      .then(onChanged)
      .catch((e: Error) => setError(e.message));
  };

  const engines: { id: TranscribeEngine; label: string; ready: boolean; why: string }[] = [
    { id: "local", label: "This Mac", ready: tx.local_available, why: "Transcribing on this Mac needs a Mac with MLX" },
    { id: "gemini", label: "Google", ready: tx.gemini_ready, why: "Add a Google API key in Settings → Transcription (and Lockdown off)" },
    { id: "remote", label: "Endpoint", ready: tx.remote_ready, why: "Set a transcription endpoint in Settings → Transcription" },
  ];

  const pickEngine = (e: TranscribeEngine) => {
    const o = engines.find((x) => x.id === e)!;
    if (!o.ready) return setError(o.why);
    save({ transcribe_engine: e });
  };

  const pickKind = (local: boolean) => {
    if (notes.local === local) return;
    const fits = providers.filter((p) => p.local === local);
    const best = fits.find((p) => p.ready) ?? fits[0];
    if (!best) return;
    if (!best.ready) return setError(best.reason);
    save({ summary_provider: best.id });
  };

  return (
    <div ref={box} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="dialog"
        className="field flex h-9 w-full min-w-0 items-center justify-between gap-2 border-transparent bg-forest/[.045] py-0 text-left text-ui shadow-none hover:border-line"
        data-tip={open ? undefined : "Where the AI runs, and which model"}
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="text-forest">{notes.local ? <LaptopIcon size={15} /> : <CloudIcon size={15} />}</span>
          <span className="shrink-0 font-medium">{notes.local ? "AI on this Mac" : "Cloud AI"}</span>
          <span className="min-w-0 truncate text-ink-soft">· {shortModel(notes.model || notes.label)}</span>
        </span>
        <ChevronDownIcon size={16} className={`shrink-0 text-ink-faint transition-transform duration-200 ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="AI defaults"
          className="absolute left-0 top-full z-30 mt-1.5 w-[312px] animate-pop rounded-lg border border-line bg-surface p-3.5 shadow-lg"
        >
          <div className="eyebrow mb-1.5">Transcripts</div>
          <Segmented
            size="sm"
            label="Transcribe with"
            value={tx.engine}
            onChange={pickEngine}
            options={engines.map((e) => ({
              id: e.id,
              label: e.label,
              icon: e.id === "local" ? <LaptopIcon size={13} /> : <CloudIcon size={13} />,
            }))}
          />

          <div className="eyebrow mb-1.5 mt-4">Notes</div>
          <Segmented
            size="sm"
            label="Write notes"
            value={where(notes.local)}
            onChange={(v) => pickKind(v === "here")}
            options={[
              { id: "here", label: "This Mac", icon: <LaptopIcon size={13} /> },
              { id: "cloud", label: "Cloud", icon: <CloudIcon size={13} /> },
            ]}
          />
          <div className="mt-2 space-y-2">
            <Select
              label="Notes AI"
              size="sm"
              value={notes.id}
              onChange={(v) => {
                const p = providers.find((x) => x.id === v);
                if (p && !p.ready) return setError(p.reason);
                save({ summary_provider: v as ProviderId });
              }}
              className="w-full"
              options={providers
                .filter((p) => p.local === notes.local)
                .map((p) => ({ value: p.id, label: p.label, hint: p.ready ? p.model : "Not set up yet" }))}
            />
            <ModelSelect
              provider={notes.id}
              value={notes.model}
              onChange={(m) => save({ [`${notes.id}_model`]: m } as Parameters<typeof api.putSettings>[0])}
            />
          </div>

          <label className="mt-4 flex cursor-pointer items-center justify-between gap-3 border-t border-line pt-3 text-hint">
            <span>
              <span className="block font-medium text-ink">Ask before processing imports</span>
              <span className="block text-ink-soft">See what each choice takes first</span>
            </span>
            <Switch
              checked={health.ask_before_ai}
              onChange={(v) => save({ ask_before_ai: v })}
              label="Ask before processing imports"
            />
          </label>

          {error && (
            <div className="mt-3 flex items-start gap-2 rounded-md bg-trail-soft/70 px-2.5 py-2 text-hint text-trail-deep">
              <AlertIcon size={14} className="mt-[3px] shrink-0" />
              <span>{error}</span>
            </div>
          )}
          <button
            className="mt-3 text-hint font-medium text-forest hover:underline"
            onClick={() => {
              setOpen(false);
              onOpenSettings();
            }}
          >
            All AI settings
          </button>
        </div>
      )}
    </div>
  );
}
