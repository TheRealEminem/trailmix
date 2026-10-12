import { useEffect, useState } from "react";
import { api } from "../api";
import type { Comparison, HistoryEntry, TranscribeEngine, TranscriptVersion, Trial } from "../api";
import type { Health, Meeting } from "../api";
import { ClockIcon, RefreshIcon } from "./icons";
import { Select, Spinner, useConfirm } from "./ui";

const FIELD_NAMES: Record<string, string> = {
  title: "Title",
  transcript: "Transcript",
  summary: "Notes",
  summary_provider: "Notes AI",
  summary_model: "Notes model",
  speaker_names_json: "Speaker names",
  my_notes_json: "Your notes",
  bookmarks_json: "Flagged moments",
  workspace_id: "Workspace",
  tags_json: "Tags",
  created_at: "Date",
  transcribed_with: "Transcribed with",
  keep_audio: "Keep audio",
  audio_deleted: "Audio deleted",
};

const ENGINES: { value: TranscribeEngine; label: string }[] = [
  { value: "gemini", label: "Google Gemini (labels speakers)" },
  { value: "local", label: "This Mac (MLX Whisper)" },
  { value: "remote", label: "Remote endpoint" },
];

const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

const took = (s: number | null) => (s == null ? "" : s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);

function label(v: TranscriptVersion): string {
  return v.made_with || (v.source === "original" ? "Earlier transcript" : v.model || "Transcript");
}

/** What a history entry changed, short enough to read at a glance. */
function change(h: HistoryEntry): string {
  const short = (s: string | null) => (s == null || s === "" ? "nothing" : s.length > 60 ? `${s.length} characters` : `“${s}”`);
  if (h.field === "transcript" || h.field === "summary") return `${short(h.old_value)} → ${short(h.new_value)}`;
  return `${short(h.old_value)} → ${short(h.new_value)}`;
}

/**
 * Every transcript made of a meeting, side by side: switch between them, try another speech model or engine on the
 * saved audio (the result is kept beside the others, nothing is replaced), compare any two word by word, and see
 * the receipt of everything that changed in the meeting.
 */
export default function Versions({
  meeting: m,
  health,
  onChanged,
}: {
  meeting: Meeting;
  health: Health | null;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<TranscriptVersion[]>([]);
  const [trial, setTrial] = useState<Trial | null>(null);
  const [engine, setEngine] = useState<TranscribeEngine>("gemini");
  const [model, setModel] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [history, setHistory] = useState<HistoryEntry[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, confirmDialog] = useConfirm();

  const audioGone = m.audio_deleted || m.audio_bytes === 0;
  const finished = ["done", "error", "ready_summarize"].includes(m.status);
  const trying = trial?.state === "queued" || trial?.state === "running";

  const load = () =>
    api
      .versions(m.id)
      .then((r) => {
        setVersions(r.versions);
        setTrial(r.trial);
      })
      .catch(() => undefined);

  useEffect(() => {
    setComparison(null);
    setPicked([]);
    setHistory(null);
    void load();
  }, [m.id, m.transcribed_with]); // eslint-disable-line react-hooks/exhaustive-deps

  // While another transcript is being made, check on it.
  useEffect(() => {
    if (!trying) return;
    const t = setInterval(() => void load(), 2000);
    return () => clearInterval(t);
  }, [trying, m.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!m.transcript || !finished) return null;

  const run = async (what: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await what();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const startTrial = () =>
    run(() => api.startTrial(m.id, { engine, model: model.trim() || undefined }));

  const use = async (v: TranscriptVersion, summarize: boolean) => {
    const ok = await confirm({
      title: "Use this transcript?",
      body: summarize
        ? `The meeting switches to the transcript made with ${label(v)}, and the notes are written again from it. The current transcript and notes stay in the history.`
        : `The meeting switches to the transcript made with ${label(v)}. The one it replaces stays here, and the notes stay as they are.`,
      confirmLabel: summarize ? "Use it and rewrite the notes" : "Use this transcript",
    });
    if (!ok) return;
    await run(() => api.applyVersion(m.id, v.id, summarize));
    setComparison(null);
    onChanged();
  };

  const toggle = (id: number) => {
    setComparison(null);
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]));
  };

  const compare = () =>
    picked.length === 2 && run(async () => setComparison(await api.compareVersions(m.id, picked[0], picked[1])));

  const showHistory = () => run(async () => setHistory(await api.history(m.id)));

  const current = versions.find((v) => v.applied);
  const gemini = health?.transcription.gemini_ready;

  return (
    <section className="inset-surface mb-6 px-4 py-3.5 text-hint text-ink-soft" aria-label="Transcript versions">
      {confirmDialog}
      <button
        className="flex w-full items-center justify-between gap-3 text-left"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="flex flex-wrap items-center gap-2">
          <span className="eyebrow">Versions</span>
          <span>
            {versions.length > 1 ? `${versions.length} transcripts` : "1 transcript"}
            {current ? ` · in use: ${label(current)}` : ""}
          </span>
          {trying && (
            <span className="inline-flex items-center gap-1.5 text-forest-deep">
              <Spinner /> {trial?.detail || "Making another transcript…"}
            </span>
          )}
        </span>
        <span className="text-ink-faint">{open ? "Hide" : "Compare and try other models"}</span>
      </button>

      {open && (
        <div className="mt-3 space-y-4">
          <ul className="divide-y divide-line rounded-xl border border-line">
            {versions.map((v) => (
              <li key={v.id} className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-2.5">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-[var(--forest)]"
                  checked={picked.includes(v.id)}
                  onChange={() => toggle(v.id)}
                  aria-label={`Compare ${label(v)}`}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2 text-label font-medium text-ink">
                    {label(v)}
                    {v.applied && <span className="chip bg-forest-soft text-forest-deep">in use</span>}
                  </div>
                  <div className="text-meta">
                    {when(v.created_at)} · {v.words.toLocaleString()} words
                    {v.speakers.length > 0 && ` · ${v.speakers.length} ${v.speakers.length === 1 ? "speaker" : "speakers"}`}
                    {v.seconds != null && ` · took ${took(v.seconds)}`}
                  </div>
                </div>
                {!v.applied && (
                  <div className="flex gap-1.5">
                    <button className="btn btn-sm btn-soft" disabled={busy} onClick={() => void use(v, false)}>
                      Use
                    </button>
                    <button
                      className="btn btn-sm btn-ghost"
                      disabled={busy}
                      onClick={() => void use(v, true)}
                      data-tip="Switch to it and write the notes again from it"
                    >
                      Use + new notes
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>

          <div className="flex flex-wrap items-center gap-2">
            <button className="btn btn-sm btn-primary" disabled={picked.length !== 2 || busy} onClick={() => void compare()}>
              Compare the two ticked
            </button>
            {picked.length < 2 && <span>Tick two transcripts to see how they differ.</span>}
          </div>

          {comparison && (
            <div className="rounded-xl border border-line p-3.5">
              <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
                <span className="text-lead font-semibold text-ink">{Math.round(comparison.agreement * 1000) / 10}% agree</span>
                <span>
                  <span className="chip bg-trail-soft text-trail-deep">A</span> {label(comparison.a)} ·{" "}
                  {comparison.a_words.toLocaleString()} words · {comparison.a.speakers.length || "no"} speakers
                </span>
                <span>
                  <span className="chip bg-forest-soft text-forest-deep">B</span> {label(comparison.b)} ·{" "}
                  {comparison.b_words.toLocaleString()} words · {comparison.b.speakers.length || "no"} speakers
                </span>
              </div>
              <p className="mb-2 text-meta">
                <span className="rounded bg-trail-soft px-1 text-trail-deep line-through">only in A</span>{" "}
                <span className="rounded bg-forest-soft px-1 text-forest-deep">only in B</span>. Words that agree are shortened.
              </p>
              <p data-private="transcript" className="max-h-96 overflow-y-auto text-body leading-relaxed text-ink">
                {comparison.inline.map((piece, i) =>
                  piece.op === "equal" ? (
                    <span key={i}>{piece.text} </span>
                  ) : piece.op === "skip" ? (
                    <span key={i} className="text-meta italic text-ink-faint">
                      {piece.text}{" "}
                    </span>
                  ) : piece.op === "delete" ? (
                    <span key={i} className="rounded bg-trail-soft px-0.5 text-trail-deep line-through">
                      {piece.text}
                    </span>
                  ) : (
                    <span key={i} className="rounded bg-forest-soft px-0.5 text-forest-deep">
                      {piece.text}
                    </span>
                  ),
                )}
              </p>
            </div>
          )}

          <div className="rounded-xl border border-line p-3.5">
            <div className="mb-2 flex items-center gap-2 text-label font-medium text-ink">
              <RefreshIcon size={16} /> Try another engine or model
            </div>
            {audioGone ? (
              <p>
                The audio has been deleted, so it can't be transcribed again. Use “Keep forever” on meetings you want to
                test models on.
              </p>
            ) : (
              <>
                <p className="mb-2.5">
                  Transcribes the saved audio again and keeps the result here next to the others. The meeting isn't
                  changed until you pick one.
                  {engine === "gemini" && !gemini && " Add a Google API key in Settings → Transcription first."}
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <Select
                    label="Engine"
                    size="sm"
                    value={engine}
                    onChange={(v) => setEngine(v as TranscribeEngine)}
                    options={ENGINES}
                    className="w-60"
                  />
                  {engine !== "local" && (
                    <input
                      className="field h-8 w-52 px-2.5 text-label"
                      placeholder={engine === "gemini" ? "Model (default: Settings)" : "Model (default: Settings)"}
                      value={model}
                      onChange={(e) => setModel(e.target.value)}
                      aria-label="Model"
                    />
                  )}
                  <button className="btn btn-sm btn-soft" disabled={busy || trying} onClick={() => void startTrial()}>
                    {trying ? "Working…" : "Transcribe to compare"}
                  </button>
                </div>
              </>
            )}
            {trial?.state === "failed" && <p className="mt-2 text-trail-deep">{trial.error}</p>}
            {trial?.state === "done" && !trying && <p className="mt-2 text-forest-deep">Done: it's in the list above.</p>}
          </div>

          <div>
            <button className="inline-flex items-center gap-1.5 hover:text-ink" onClick={() => void showHistory()}>
              <ClockIcon size={16} /> {history ? "Change history" : "Show the change history"}
            </button>
            {history && (
              <ul className="mt-2 max-h-72 space-y-1.5 overflow-y-auto" data-private="history">
                {history.length === 0 && <li>Nothing has changed yet.</li>}
                {history.map((h) => (
                  <li key={h.id}>
                    <span className="tabular-nums text-ink-faint">{when(h.at)}</span>{" "}
                    <span className="text-ink">{FIELD_NAMES[h.field] ?? h.field}</span>: {change(h)}
                    {h.why ? <span className="text-ink-faint"> ({h.why})</span> : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
          {error && <p className="text-trail-deep">{error}</p>}
        </div>
      )}
    </section>
  );
}
