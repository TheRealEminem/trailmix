import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { GranolaNote, ImportJob } from "../api";
import { formatLongDate } from "../format";
import { CheckIcon, DownloadIcon } from "./icons";
import { PageHeader, Spinner } from "./ui";

interface Props {
  onImported: () => void;
  onOpenMeeting: (id: number) => void;
  onError: (message: string) => void;
}

/** Meetings recorded elsewhere: from Granola (its API) or as a pasted transcript or text file. */
export default function ImportPanel({
  onImported,
  onOpenMeeting,
  onError,
}: Props) {
  return (
    <div>
      <PageHeader
        title="Import meetings"
        subtitle="Bring back meetings you exported from Trailmix, or transcripts from Granola and elsewhere. They become searchable, and you can ask about them and summarize them like any meeting."
      />
      <div className="space-y-6">
        <TrailmixSection
          onImported={onImported}
          onOpenMeeting={onOpenMeeting}
          onError={onError}
        />
        <GranolaSection onImported={onImported} onError={onError} />
        <PasteSection
          onImported={onImported}
          onOpenMeeting={onOpenMeeting}
          onError={onError}
        />
      </div>
    </div>
  );
}

function GranolaSection({
  onImported,
  onError,
}: Pick<Props, "onImported" | "onError">) {
  const [hint, setHint] = useState<string | null | undefined>(undefined);
  const [key, setKey] = useState("");
  const [notes, setNotes] = useState<GranolaNote[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [keepSummary, setKeepSummary] = useState(true);
  const [job, setJob] = useState<ImportJob | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    api
      .getSettings()
      .then((s) => setHint(s.secret_hints?.granola_api_key ?? null))
      .catch(() => setHint(null));
    api
      .granolaImportStatus()
      .then((j) => j.active && setJob(j))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!job?.active) return;
    const t = setInterval(() => {
      api.granolaImportStatus().then((j) => {
        setJob(j);
        if (!j.active) {
          onImported();
          void load();
        }
      });
    }, 1000);
    return () => clearInterval(t);
  }, [job?.active]); // eslint-disable-line react-hooks/exhaustive-deps

  const load = async () => {
    setLoading(true);
    setProblem(null);
    try {
      const { notes } = await api.granolaNotes();
      setNotes(notes);
      setPicked(new Set(notes.filter((n) => !n.imported).map((n) => n.id)));
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const saveKey = async () => {
    const v = key.trim();
    if (!v) return;
    try {
      const s = await api.putSettings({ granola_api_key: v });
      setHint(s.secret_hints?.granola_api_key ?? "set");
      setKey("");
      await load();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const start = async () => {
    try {
      setJob(await api.importGranola([...picked], keepSummary));
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const fresh = notes?.filter((n) => !n.imported) ?? [];
  const toggle = (id: string) =>
    setPicked((p) => {
      const next = new Set(p);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <section
      className="panel animate-enter p-5 sm:p-7"
      aria-labelledby="granola-heading"
    >
      <h2 id="granola-heading" className="font-display text-lead font-semibold">
        From Granola
      </h2>
      <p className="mt-1 text-label leading-relaxed text-ink-soft">
        Imports each meeting's transcript (with You and Them) and, if you like,
        Granola's notes as the summary. Granola's API needs a Business or
        Enterprise plan; on other plans, use “Paste a transcript” below.
      </p>

      {hint === undefined ? null : !hint ? (
        <div className="mt-4 rounded-lg border border-line bg-surface-subtle/50 p-4 text-label">
          <ol className="list-decimal space-y-1 pl-5 text-ink-soft">
            <li>
              In Granola, open{" "}
              <b className="text-ink">Settings → Connectors → API keys</b>.
            </li>
            <li>
              Click <b className="text-ink">Create new key</b>, give it access
              to your notes, and click{" "}
              <b className="text-ink">Generate API Key</b>.
            </li>
            <li>Copy the key (it starts with grn_) and paste it here.</li>
          </ol>
          <div className="mt-3 flex flex-wrap gap-2">
            <input
              type="password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void saveKey()}
              placeholder="grn_…"
              aria-label="Granola API key"
              autoComplete="off"
              spellCheck={false}
              className="field min-w-0 flex-1 font-mono text-hint sm:w-72 sm:flex-none"
            />
            <button
              className="btn btn-sm btn-primary"
              disabled={!key.trim()}
              onClick={() => void saveKey()}
            >
              Save and load my meetings
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-4">
          {!notes && (
            <div className="flex flex-wrap items-center gap-3">
              <button
                className="btn btn-md btn-primary"
                onClick={() => void load()}
                disabled={loading}
              >
                {loading ? <Spinner /> : <DownloadIcon size={16} />}
                {loading
                  ? "Loading your Granola meetings…"
                  : "Load my Granola meetings"}
              </button>
              <span className="text-hint text-ink-soft">
                Key saved ({hint}). Change it in Settings → Import.
              </span>
            </div>
          )}
          {problem && (
            <p className="mt-3 text-label text-trail-deep">{problem}</p>
          )}

          {job && (job.active || (job.done ?? 0) > 0) && (
            <div
              className="mt-4 rounded-lg border border-sky/25 bg-sky-soft/60 p-4 text-label text-sky-deep"
              role="status"
            >
              {job.active ? (
                <>
                  Importing {job.done} of {job.total}…
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-sky/15">
                    <div
                      className="h-full rounded-full bg-sky transition-[width] duration-500"
                      style={{
                        width: `${((job.done ?? 0) / (job.total || 1)) * 100}%`,
                      }}
                    />
                  </div>
                </>
              ) : (
                <>
                  <span className="font-semibold">
                    Imported {job.imported} meeting
                    {job.imported === 1 ? "" : "s"}.
                  </span>
                  {(job.skipped ?? 0) > 0 &&
                    ` ${job.skipped} skipped (already here, or no transcript).`}
                  {!keepSummary && " Trailmix is writing their summaries now."}
                  {(job.errors?.length ?? 0) > 0 && (
                    <div className="mt-1 text-trail-deep">{job.errors![0]}</div>
                  )}
                </>
              )}
            </div>
          )}

          {notes && !job?.active && (
            <div className="mt-2">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-3">
                <span className="text-label text-ink-soft">
                  {notes.length} meeting{notes.length === 1 ? "" : "s"} in
                  Granola, {fresh.length} not imported yet.
                </span>
                <div className="flex gap-2">
                  <button
                    className="btn btn-sm btn-ghost"
                    onClick={() => setPicked(new Set(fresh.map((n) => n.id)))}
                  >
                    Select all new
                  </button>
                  <button
                    className="btn btn-sm btn-ghost"
                    onClick={() => setPicked(new Set())}
                  >
                    Clear
                  </button>
                </div>
              </div>
              <ul className="max-h-80 divide-y divide-line overflow-y-auto">
                {notes.map((n) => (
                  <li key={n.id}>
                    <label
                      className={`flex items-center gap-3 py-2.5 ${n.imported ? "opacity-60" : "cursor-pointer"}`}
                    >
                      {n.imported ? (
                        <CheckIcon size={16} className="shrink-0 text-forest" />
                      ) : (
                        <input
                          type="checkbox"
                          checked={picked.has(n.id)}
                          onChange={() => toggle(n.id)}
                          className="h-4 w-4 shrink-0 accent-[var(--forest)]"
                        />
                      )}
                      <span className="min-w-0 flex-1 truncate text-label">
                        {n.title}
                      </span>
                      <span className="shrink-0 text-hint tabular-nums text-ink-soft">
                        {n.imported ? "Imported" : formatLongDate(n.created_at)}
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-4">
                <label className="flex cursor-pointer items-center gap-2 text-label">
                  <input
                    type="checkbox"
                    checked={keepSummary}
                    onChange={(e) => setKeepSummary(e.target.checked)}
                    className="h-4 w-4 accent-[var(--forest)]"
                  />
                  Keep Granola's notes as the summary
                  <span className="text-hint text-ink-soft">
                    (off: Trailmix writes new ones)
                  </span>
                </label>
                <button
                  className="btn btn-md btn-primary"
                  disabled={!picked.size}
                  onClick={() => void start()}
                >
                  <DownloadIcon size={16} />
                  Import {picked.size} meeting{picked.size === 1 ? "" : "s"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function PasteSection({ onImported, onOpenMeeting, onError }: Props) {
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [busy, setBusy] = useState(false);
  const [dropped, setDropped] = useState<string | null>(null);
  const files = useRef<HTMLInputElement>(null);

  const importOne = async () => {
    setBusy(true);
    try {
      const { id } = await api.importText(text, title, date);
      setText("");
      setTitle("");
      onImported();
      onOpenMeeting(id);
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const importFiles = async (list: FileList | null) => {
    if (!list?.length) return;
    setBusy(true);
    let made = 0;
    try {
      for (const f of Array.from(list)) {
        const body = await f.text();
        if (!body.trim()) continue;
        await api.importText(
          body,
          f.name.replace(/\.(txt|md|markdown)$/i, ""),
          new Date(f.lastModified).toISOString(),
        );
        made++;
      }
      setDropped(`Imported ${made} file${made === 1 ? "" : "s"}.`);
      onImported();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      className="panel animate-enter p-5 sm:p-7"
      aria-labelledby="paste-heading"
      style={{ animationDelay: "60ms" }}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        void importFiles(e.dataTransfer.files);
      }}
    >
      <h2 id="paste-heading" className="font-display text-lead font-semibold">
        Paste a transcript
      </h2>
      <p className="mt-1 text-label leading-relaxed text-ink-soft">
        Works with any Granola plan: open a meeting in Granola, click the
        transcript, then <b className="text-ink">Copy transcript</b>, and paste
        it here. Lines like “Me: …” and “Them: …” (or names) become You and
        Them. You can also drop .txt or .md files onto this box.
      </p>
      <div className="mt-4 flex flex-wrap gap-3">
        <input
          className="field min-w-0 flex-1"
          placeholder="Title (optional: Trailmix can name it)"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          aria-label="Title"
        />
        <input
          type="date"
          className="field w-44"
          value={date}
          onChange={(e) => setDate(e.target.value)}
          aria-label="Date of the meeting"
        />
      </div>
      <textarea
        className="field mt-3 h-48 w-full resize-y font-mono text-hint leading-relaxed"
        placeholder={
          "Me: Shall we ship on Tuesday?\nThem: Yes, and Priya will update the docs by Monday."
        }
        value={text}
        onChange={(e) => setText(e.target.value)}
        aria-label="Transcript"
      />
      <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <button
            className="btn btn-sm btn-ghost"
            onClick={() => files.current?.click()}
            disabled={busy}
          >
            Choose files…
          </button>
          <input
            ref={files}
            type="file"
            accept=".txt,.md,.markdown,text/plain"
            multiple
            hidden
            onChange={(e) => void importFiles(e.target.files)}
          />
          {dropped && <span className="text-hint text-forest">{dropped}</span>}
        </div>
        <button
          className="btn btn-md btn-primary"
          disabled={!text.trim() || busy}
          onClick={() => void importOne()}
        >
          {busy ? <Spinner /> : <DownloadIcon size={16} />}
          Import transcript
        </button>
      </div>
    </section>
  );
}

/** Meetings exported from Trailmix (a folder per meeting, with meeting.json): after reinstalling, on a new
 *  Mac, or from someone else's Trailmix. */
function TrailmixSection({ onImported, onOpenMeeting, onError }: Props) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const folder = useRef<HTMLInputElement>(null);

  useEffect(() => {
    folder.current?.setAttribute("webkitdirectory", ""); // React doesn't know this attribute
  }, []);

  const upload = async (list: FileList | null) => {
    if (!list?.length) return;
    setBusy(true);
    setResult(null);
    try {
      const r = await api.importTrailmix(Array.from(list));
      const parts = [
        `Imported ${r.imported.length} meeting${r.imported.length === 1 ? "" : "s"}.`,
      ];
      if (r.skipped) parts.push(`${r.skipped} already here, skipped.`);
      if (r.problems.length)
        parts.push(`${r.problems.length} couldn't be read: ${r.problems[0]}`);
      setResult(parts.join(" "));
      onImported();
      if (r.imported.length === 1) onOpenMeeting(r.imported[0]);
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
      if (folder.current) folder.current.value = "";
    }
  };

  return (
    <section
      className="panel animate-enter p-5 sm:p-7"
      aria-labelledby="trailmix-import-heading"
    >
      <h2
        id="trailmix-import-heading"
        className="font-display text-lead font-semibold"
      >
        From Trailmix
      </h2>
      <p className="mt-1 text-label leading-relaxed text-ink-soft">
        Choose your Trailmix export folder (or one meeting's folder in it). Each
        meeting comes back exactly as it was: the notes, transcript, tasks and
        their ticks, flagged moments, names, and the recording if it was
        exported. Meetings already here are skipped.
      </p>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          className="btn btn-md btn-primary"
          onClick={() => folder.current?.click()}
          disabled={busy}
        >
          {busy ? <Spinner /> : <DownloadIcon size={16} />}
          {busy ? "Importing…" : "Choose a folder…"}
        </button>
        <input
          ref={folder}
          type="file"
          multiple
          hidden
          onChange={(e) => void upload(e.target.files)}
        />
        {result && <span className="text-label text-forest">{result}</span>}
      </div>
    </section>
  );
}
