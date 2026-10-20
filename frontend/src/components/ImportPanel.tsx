import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, inApp, revealInFinder } from "../api";
import type { Connection, Connections, GranolaNote, ImportJob, SyncStart } from "../api";
import { formatLongDate, formatTime, tildePath } from "../format";
import {
  CheckIcon,
  ChevronDownIcon,
  CloseIcon,
  DownloadIcon,
  FolderIcon,
  KeyIcon,
  PlusIcon,
  RefreshIcon,
} from "./icons";
import { Collapse, PageHeader, Segmented, Spinner, Switch, useConfirm } from "./ui";

interface Props {
  onImported: () => void;
  onOpenMeeting: (id: number) => void;
  onError: (message: string) => void;
}

/** Meetings recorded elsewhere. Connected: what brings meetings in by itself (Granola, watched folders), all one
 * list. Import once: one place for files, Trailmix exports and pasted transcripts. */
export default function ImportPanel({
  onImported,
  onOpenMeeting,
  onError,
}: Props) {
  return (
    <div>
      <PageHeader
        title="Import meetings"
        subtitle="Bring in meetings recorded with other apps. They become searchable, and you can ask about them and summarize them like any meeting."
      />
      <div className="space-y-6">
        <Connected onImported={onImported} onError={onError} />
        <ImportOnce
          onImported={onImported}
          onOpenMeeting={onOpenMeeting}
          onError={onError}
        />
      </div>
    </div>
  );
}

// ── Connected ─────────────────────────────────────────────────────────

function Connected({ onImported, onError }: Pick<Props, "onImported" | "onError">) {
  const [data, setData] = useState<Connections | null>(null);
  const [adding, setAdding] = useState(false);
  const total = useRef<number | null>(null);

  const take = (d: Connections) => {
    setData(d);
    const n = d.connections.reduce((sum, c) => sum + c.total, 0);
    if (total.current !== null && n > total.current) onImported();
    total.current = n;
  };
  const load = () =>
    api
      .importConnections()
      .then(take)
      .catch(() => undefined);

  // Quicker while something is being checked or a new file is settling
  const busy = !!data?.connections.some((c) => c.running || c.waiting > 0);
  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), busy ? 3000 : 20000);
    return () => clearInterval(t);
  }, [busy]); // eslint-disable-line react-hooks/exhaustive-deps

  const rows = data?.connections ?? [];
  return (
    <section
      className="panel animate-enter p-5 sm:p-7"
      aria-labelledby="connected-heading"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2
            id="connected-heading"
            className="font-display text-lead font-semibold"
          >
            Connected
          </h2>
          <p className="mt-1 text-label leading-relaxed text-ink-soft">
            New meetings from these come in by themselves while Trailmix is
            open.
          </p>
        </div>
        <button
          className="btn btn-sm btn-primary"
          onClick={() => setAdding((a) => !a)}
          aria-expanded={adding}
        >
          {adding ? <CloseIcon size={14} /> : <PlusIcon size={14} />}
          {adding ? "Close" : "Connect"}
        </button>
      </div>

      <Collapse open={adding}>
        {data && (
          <ConnectPicker
            available={data.available}
            onDone={(d) => {
              take(d);
              setAdding(false);
            }}
            onError={onError}
          />
        )}
      </Collapse>

      {data && rows.length === 0 && !adding && (
        <div className="mt-4 rounded-lg border border-dashed border-line p-5 text-center text-label text-ink-soft">
          Nothing connected yet. Connect Granola, or a folder where another
          app saves its recordings or transcripts.
        </div>
      )}
      {rows.length > 0 && (
        <ul className="mt-4 divide-y divide-line rounded-lg border border-line">
          {rows.map((c) => (
            <ConnectionRow
              key={c.id}
              c={c}
              onChange={take}
              onReload={() => void load()}
              onImported={onImported}
              onError={onError}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function statusLine(c: Connection): ReactNode {
  if (c.running) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <Spinner /> Checking…
      </span>
    );
  }
  if (c.error) return <span className="text-trail-deep">{c.error}</span>;
  if (c.kind === "service" && !c.auto) {
    return `Not bringing in new meetings · ${c.total} imported`;
  }
  const parts = [`${c.total} imported`];
  if (c.waiting > 0) parts.push(`${c.waiting} arriving`);
  if (c.failed > 0) parts.push(`${c.failed} couldn't be read`);
  parts.push(c.last ? `checked ${formatTime(c.last)}` : "first check in a moment");
  return parts.join(" · ");
}

function ConnectionRow({
  c,
  onChange,
  onReload,
  onImported,
  onError,
}: {
  c: Connection;
  onChange: (d: Connections) => void;
  onReload: () => void;
} & Pick<Props, "onImported" | "onError">) {
  const [open, setOpen] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [confirm, dialog] = useConfirm();
  const service = c.kind === "service";

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      onReload();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const remove = async () => {
    const ok = await confirm(
      service
        ? {
            title: `Disconnect ${c.label}?`,
            body: `Trailmix stops bringing in ${c.label} meetings and forgets its API key. The meetings it brought in stay.`,
            confirmLabel: "Disconnect",
            tone: "danger",
          }
        : {
            title: "Stop watching this folder?",
            body: "New files saved there won't come in any more. The meetings it brought in stay, and your files aren't touched.",
            confirmLabel: "Stop watching",
            tone: "danger",
          },
    );
    if (!ok) return;
    try {
      if (service) onChange(await api.disconnectImportSource(c.id));
      else {
        await api.unwatchFolder(c.path!);
        onReload();
      }
    } catch (e) {
      onError((e as Error).message);
    }
  };

  return (
    <li>
      <div className="flex items-center gap-3 p-3">
        <button
          className="flex min-w-0 flex-1 items-center gap-3 text-left"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
        >
          <span className="well">
            {service ? <KeyIcon size={16} /> : <FolderIcon size={16} />}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-label font-medium text-ink">
              {c.label}
            </span>
            <span className="block truncate text-hint text-ink-soft" role="status">
              {statusLine(c)}
            </span>
          </span>
          <ChevronDownIcon
            size={16}
            className={`shrink-0 text-ink-soft transition-transform ${open ? "rotate-180" : ""}`}
          />
        </button>
        {service && (
          <Switch
            checked={c.auto}
            onChange={(v) => void act(() => api.setImportSource(c.id, v))}
            label={`Bring in new ${c.label} meetings automatically`}
          />
        )}
      </div>

      <Collapse open={open}>
        <div className="space-y-3 border-t border-line px-3 pb-3 pt-3">
          {service ? (
            <ServiceDetails c={c} onError={onError} />
          ) : (
            <p
              className="truncate font-mono text-hint text-ink-soft"
              title={c.path!}
            >
              {tildePath(c.path!)}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <button
              className="btn btn-sm btn-ghost"
              disabled={c.running || (service && !c.auto)}
              onClick={() =>
                void act(() =>
                  service ? api.syncImportSource(c.id) : api.scanFolders(),
                )
              }
            >
              <RefreshIcon size={14} />
              Check now
            </button>
            {service && (
              <button
                className="btn btn-sm btn-ghost"
                onClick={() => setBrowsing((b) => !b)}
                aria-expanded={browsing}
              >
                <DownloadIcon size={14} />
                Pick past meetings…
              </button>
            )}
            {!service && inApp && (
              <button
                className="btn btn-sm btn-ghost"
                onClick={() => revealInFinder(c.path!)}
              >
                <FolderIcon size={14} />
                Show in Finder
              </button>
            )}
            <button className="btn btn-sm btn-ghost" onClick={() => void remove()}>
              <CloseIcon size={14} />
              {service ? "Disconnect" : "Stop watching"}
            </button>
          </div>
          {service && browsing && (
            <GranolaBrowser onImported={onImported} onError={onError} />
          )}
        </div>
      </Collapse>
      {dialog}
    </li>
  );
}

/** A service's own settings: where its automatic imports started, and whether its notes come along. */
function ServiceDetails({ c, onError }: { c: Connection } & Pick<Props, "onError">) {
  const [keepNotes, setKeepNotes] = useState(true);
  useEffect(() => {
    api
      .getSettings()
      .then((s) => setKeepNotes(s.auto_import_keep_notes))
      .catch(() => undefined);
  }, []);

  const save = async (v: boolean) => {
    setKeepNotes(v);
    try {
      await api.putSettings({ auto_import_keep_notes: v });
    } catch (e) {
      setKeepNotes(!v);
      onError((e as Error).message);
    }
  };

  return (
    <>
      {c.auto && (
        <p className="text-hint text-ink-soft">
          Checks every 15 minutes, bringing in meetings{" "}
          {c.since ? `from ${formatLongDate(c.since)} on` : "of any date"}.
          {(c.last_count ?? 0) > 0 &&
            ` The last check brought in ${c.last_count}.`}
        </p>
      )}
      <label className="flex cursor-pointer items-center gap-2 text-label">
        <input
          type="checkbox"
          checked={keepNotes}
          onChange={(e) => void save(e.target.checked)}
          className="h-4 w-4 accent-[var(--forest)]"
        />
        Keep {c.label}'s notes as the summary
        <span className="text-hint text-ink-soft">
          (off: Trailmix writes new ones)
        </span>
      </label>
    </>
  );
}

// ── Connecting something new ──────────────────────────────────────────

function ConnectPicker({
  available,
  onDone,
  onError,
}: {
  available: Connections["available"];
  onDone: (d: Connections) => void;
} & Pick<Props, "onError">) {
  const [choice, setChoice] = useState<string>("folder");
  const options = [
    ...available.services.map((s) => ({
      id: s.name,
      label: s.label,
      icon: <KeyIcon size={14} />,
    })),
    { id: "folder", label: "A folder", icon: <FolderIcon size={14} /> },
  ];
  const service = available.services.find((s) => s.name === choice);

  return (
    <div className="mt-4 rounded-lg border border-line bg-surface-subtle/50 p-4">
      <div className="flex flex-wrap items-center gap-2 text-hint text-ink-soft">
        Connect
        <Segmented
          size="sm"
          label="What to connect"
          value={choice}
          onChange={setChoice}
          options={options}
        />
      </div>
      {service ? (
        <ConnectGranola onDone={onDone} onError={onError} />
      ) : (
        <ConnectFolder
          suggestions={available.folders}
          onDone={onDone}
          onError={onError}
        />
      )}
    </div>
  );
}

function StartChoice({
  value,
  onChange,
  noun,
}: {
  value: SyncStart;
  onChange: (v: SyncStart) => void;
  noun: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-hint text-ink-soft">
      Bring in
      <Segmented
        size="sm"
        label={`Which ${noun} to bring in`}
        value={value}
        onChange={onChange}
        options={[
          { id: "now", label: "New only" },
          { id: "week", label: "Last 7 days" },
          { id: "all", label: "Everything" },
        ]}
      />
    </div>
  );
}

function ConnectGranola({
  onDone,
  onError,
}: { onDone: (d: Connections) => void } & Pick<Props, "onError">) {
  const [key, setKey] = useState("");
  const [start, setStart] = useState<SyncStart>("now");
  const [busy, setBusy] = useState(false);

  const connect = async () => {
    setBusy(true);
    try {
      await api.putSettings({ granola_api_key: key.trim() });
      try {
        await api.setImportSource("granola", true, start);
      } catch (e) {
        await api.putSettings({ granola_api_key: "" }); // a key that doesn't work isn't kept
        throw e;
      }
      setKey("");
      onDone(await api.importConnections());
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-4 space-y-3 text-label">
      <ol className="list-decimal space-y-1 pl-5 text-ink-soft">
        <li>
          In Granola, open{" "}
          <b className="text-ink">Settings → Connectors → API keys</b>.
        </li>
        <li>
          Click <b className="text-ink">Create new key</b>, give it access to
          your notes, and click <b className="text-ink">Generate API Key</b>.
        </li>
        <li>Copy the key (it starts with grn_) and paste it here.</li>
      </ol>
      <p className="text-hint text-ink-soft">
        Granola's API needs a Business or Enterprise plan. On other plans, copy
        a meeting's transcript in Granola and paste it under Import once.
      </p>
      <input
        type="password"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && key.trim() && void connect()}
        placeholder="grn_…"
        aria-label="Granola API key"
        autoComplete="off"
        spellCheck={false}
        className="field w-full font-mono text-hint sm:w-80"
      />
      <StartChoice value={start} onChange={setStart} noun="meetings" />
      <button
        className="btn btn-sm btn-primary"
        disabled={!key.trim() || busy}
        onClick={() => void connect()}
      >
        {busy ? <Spinner /> : <KeyIcon size={14} />}
        Connect Granola
      </button>
    </div>
  );
}

function ConnectFolder({
  suggestions,
  onDone,
  onError,
}: {
  suggestions: Connections["available"]["folders"];
  onDone: (d: Connections) => void;
} & Pick<Props, "onError">) {
  const [path, setPath] = useState("");
  const [existing, setExisting] = useState(false);
  const [busy, setBusy] = useState(false);

  const watch = async (p: string) => {
    if (!p.trim()) return;
    setBusy(true);
    try {
      await api.watchFolder(p.trim(), existing);
      setPath("");
      onDone(await api.importConnections());
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-4 space-y-3 text-label">
      <p className="text-hint leading-relaxed text-ink-soft">
        New recordings and transcripts saved into the folder come in by
        themselves, like Zoom's recordings, or Meet and Teams transcripts synced
        by Google Drive or OneDrive. Trailmix looks every minute and only reads
        your files. Text files come in only if they look like a transcript.
      </p>
      {suggestions.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-hint text-ink-soft">
          Found on this Mac:
          {suggestions.map((s) => (
            <button
              key={s.path}
              className="btn btn-sm btn-ghost"
              title={s.path}
              disabled={busy}
              onClick={() => void watch(s.path)}
            >
              <FolderIcon size={14} />
              {s.label}
            </button>
          ))}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void watch(path)}
          placeholder="~/Documents/Zoom"
          aria-label="Folder to watch"
          spellCheck={false}
          className="field min-w-0 flex-1 font-mono text-hint"
        />
        <button
          className="btn btn-sm btn-primary"
          disabled={!path.trim() || busy}
          onClick={() => void watch(path)}
        >
          {busy ? <Spinner /> : <FolderIcon size={14} />}
          Watch folder
        </button>
      </div>
      <label className="flex cursor-pointer items-center gap-2">
        <input
          type="checkbox"
          checked={existing}
          onChange={(e) => setExisting(e.target.checked)}
          className="h-4 w-4 accent-[var(--forest)]"
        />
        Also import what's in it now
        <span className="text-hint text-ink-soft">
          (off: only files that arrive from now on)
        </span>
      </label>
    </div>
  );
}

/** Picking past Granola meetings by hand (the automatic import only brings in new ones). */
function GranolaBrowser({ onImported, onError }: Pick<Props, "onImported" | "onError">) {
  const [notes, setNotes] = useState<GranolaNote[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [keepSummary, setKeepSummary] = useState(true);
  const [job, setJob] = useState<ImportJob | null>(null);

  const load = async () => {
    setProblem(null);
    try {
      const { notes } = await api.granolaNotes();
      setNotes(notes);
      setPicked(new Set(notes.filter((n) => !n.imported).map((n) => n.id)));
    } catch (e) {
      setProblem((e as Error).message);
    }
  };

  useEffect(() => {
    void load();
    api
      .granolaImportStatus()
      .then((j) => j.active && setJob(j))
      .catch(() => undefined);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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

  if (problem) return <p className="text-label text-trail-deep">{problem}</p>;
  if (!notes) {
    return (
      <p className="inline-flex items-center gap-2 text-label text-ink-soft">
        <Spinner /> Loading your Granola meetings…
      </p>
    );
  }

  return (
    <div className="rounded-lg border border-line p-3">
      {job && (job.active || (job.done ?? 0) > 0) && (
        <div
          className="mb-3 rounded-lg border border-sky/25 bg-sky-soft/60 p-3 text-label text-sky-deep"
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
                Imported {job.imported} meeting{job.imported === 1 ? "" : "s"}.
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

      {!job?.active && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-3">
            <span className="text-label text-ink-soft">
              {notes.length} meeting{notes.length === 1 ? "" : "s"} in Granola,{" "}
              {fresh.length} not imported yet.
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
          <ul
            data-private="Granola notes"
            className="max-h-80 divide-y divide-line overflow-y-auto"
          >
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
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
            <label className="flex cursor-pointer items-center gap-2 text-label">
              <input
                type="checkbox"
                checked={keepSummary}
                onChange={(e) => setKeepSummary(e.target.checked)}
                className="h-4 w-4 accent-[var(--forest)]"
              />
              Keep Granola's notes as the summary
            </label>
            <button
              className="btn btn-sm btn-primary"
              disabled={!picked.size}
              onClick={() => void start()}
            >
              <DownloadIcon size={14} />
              Import {picked.size} meeting{picked.size === 1 ? "" : "s"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// ── Import once ───────────────────────────────────────────────────────

const RECORDINGS =
  ".mp3,.m4a,.wav,.aac,.flac,.ogg,.opus,.oga,.webm,.mp4,.mov,.mkv,.m4v,.avi,.wma,.aif,.aiff,.caf,.amr,.3gp,.mpeg,.mpg";
const TRANSCRIPTS = ".vtt,.srt,.txt,.docx,.md";

/** One place for everything brought in by hand: recordings and transcript files, a Trailmix export folder, or a
 * pasted transcript. Trailmix tells which is which. */
function ImportOnce({ onImported, onOpenMeeting, onError }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const [pasting, setPasting] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const folder = useRef<HTMLInputElement>(null);

  useEffect(() => {
    folder.current?.setAttribute("webkitdirectory", ""); // React doesn't know this attribute
  }, []);

  const files = async (list: File[]) => {
    if (!list.length) return;
    setResult(null);
    const made: number[] = [];
    let recordings = 0;
    let skipped = 0;
    const problems: string[] = [];
    for (const [i, f] of list.entries()) {
      setBusy(`${list.length > 1 ? `${i + 1} of ${list.length}: ` : ""}${f.name}`);
      try {
        const r = await api.importFile(f);
        if (r.skipped) skipped++;
        else if (r.id) {
          made.push(r.id);
          if (r.kind === "recording") recordings++;
        }
      } catch (e) {
        problems.push((e as Error).message);
      }
    }
    setBusy(null);
    if (picker.current) picker.current.value = "";
    const parts = [];
    if (made.length)
      parts.push(`Imported ${made.length} meeting${made.length === 1 ? "" : "s"}.`);
    if (recordings)
      parts.push(
        `${recordings === 1 ? "The recording is" : "Recordings are"} being transcribed now; the notes follow.`,
      );
    if (skipped) parts.push(`${skipped} already here, skipped.`);
    setResult(parts.join(" ") || null);
    if (problems.length) onError(problems.join(" · "));
    if (made.length) onImported();
    if (made.length === 1) onOpenMeeting(made[0]);
  };

  const trailmix = async (list: File[]) => {
    setBusy("Your Trailmix export");
    setResult(null);
    try {
      const r = await api.importTrailmix(list);
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
      setBusy(null);
      if (folder.current) folder.current.value = "";
    }
  };

  // A Trailmix export has a meeting.json in each meeting's folder; anything else is files from other apps
  const take = (list: File[]) =>
    void (list.some((f) => f.name === "meeting.json") ? trailmix(list) : files(list));

  return (
    <section
      className={`panel animate-enter p-5 transition-[box-shadow] sm:p-7 ${over ? "ring-2 ring-forest/40" : ""}`}
      aria-labelledby="import-once-heading"
      style={{ animationDelay: "60ms" }}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        take(Array.from(e.dataTransfer.files));
      }}
    >
      <h2
        id="import-once-heading"
        className="font-display text-lead font-semibold"
      >
        Import once
      </h2>
      <p className="mt-1 text-label leading-relaxed text-ink-soft">
        Drop files here, or choose them.{" "}
        <b className="font-medium text-ink">Recordings</b> (audio or video) are
        transcribed and summarized like a meeting you recorded: Trailmix keeps
        only the audio, for 30 days unless you keep it forever, and never
        changes your file. <b className="font-medium text-ink">Transcripts</b>{" "}
        from Zoom, Teams or Meet (VTT), subtitles (SRT), Otter (TXT or DOCX) or
        plain text are summarized. A{" "}
        <b className="font-medium text-ink">Trailmix export folder</b> comes
        back exactly as it was.
      </p>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          className="btn btn-md btn-primary"
          onClick={() => picker.current?.click()}
          disabled={!!busy}
        >
          {busy ? <Spinner /> : <DownloadIcon size={16} />}
          {busy ? "Importing…" : "Choose files…"}
        </button>
        <button
          className="btn btn-md btn-ghost"
          onClick={() => folder.current?.click()}
          disabled={!!busy}
        >
          <FolderIcon size={16} />
          Trailmix export folder…
        </button>
        <button
          className="btn btn-md btn-ghost"
          onClick={() => setPasting((p) => !p)}
          aria-expanded={pasting}
        >
          Paste a transcript
        </button>
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          accept={`${RECORDINGS},${TRANSCRIPTS}`}
          onChange={(e) => void files(Array.from(e.target.files ?? []))}
        />
        <input
          ref={folder}
          type="file"
          multiple
          hidden
          onChange={(e) => void trailmix(Array.from(e.target.files ?? []))}
        />
      </div>
      {busy && <p className="mt-3 truncate text-label text-ink-soft">{busy}</p>}
      {!busy && result && <p className="mt-3 text-label text-forest">{result}</p>}
      <Collapse open={pasting}>
        <PasteForm
          onImported={onImported}
          onOpenMeeting={onOpenMeeting}
          onError={onError}
        />
      </Collapse>
    </section>
  );
}

function PasteForm({ onImported, onOpenMeeting, onError }: Props) {
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [date, setDate] = useState(() => {
    const d = new Date(); // today here (toISOString would give the date in UTC: tomorrow, in the evening)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  });
  const [busy, setBusy] = useState(false);

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

  return (
    <div className="mt-4 border-t border-line pt-4">
      <p className="text-label leading-relaxed text-ink-soft">
        Works with any Granola plan: open a meeting in Granola, click the
        transcript, then <b className="text-ink">Copy transcript</b>, and paste
        it here. Lines like “Me: …” and “Them: …” (or names) become You and
        Them. Captions copied from YouTube or Google Drive work too.
      </p>
      <div className="mt-3 flex flex-wrap gap-3">
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
      <div className="mt-3 flex justify-end">
        <button
          className="btn btn-md btn-primary"
          disabled={!text.trim() || busy}
          onClick={() => void importOne()}
        >
          {busy ? <Spinner /> : <DownloadIcon size={16} />}
          Import transcript
        </button>
      </div>
    </div>
  );
}
