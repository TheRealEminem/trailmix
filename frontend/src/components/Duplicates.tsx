import { useState } from "react";
import { api } from "../api";
import type { DuplicatePair, Meeting } from "../api";
import { formatLongDate, formatTime } from "../format";
import { ColumnsIcon, RefreshIcon } from "./icons";
import { Spinner } from "./ui";

function moved(seconds: number) {
  const s = Math.round(Math.abs(seconds));
  if (s < 2) return "";
  const text = s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `${s} s`;
  return `, moved ${text} to line up with the recording`;
}

/**
 * On a meeting: that a copy of it was merged in (with Undo), or that it looks like the same meeting as another one
 * (Merge / Not the same), when you'd rather be asked.
 */
export function DuplicateNotice({
  meeting: m,
  onOpenMeeting,
  onChanged,
  onError,
}: {
  meeting: Meeting;
  onOpenMeeting: (id: number) => void;
  onChanged: () => void;
  onError: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const last = m.merged[m.merged.length - 1];
  if (!last && !m.duplicate_of) return null;

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  return (
    <div className="panel mb-6 flex animate-enter flex-wrap items-center justify-between gap-3 px-4 py-3 text-hint">
      <div className="flex min-w-0 flex-1 basis-72 items-start gap-3">
        <span className="well h-8 w-8 bg-sky-soft text-sky-deep">
          <ColumnsIcon size={16} />
        </span>
        {m.duplicate_of ? (
          <p className="leading-relaxed text-ink-soft">
            <span className="font-medium text-ink">Looks like the same meeting as “{m.duplicate_of.title}”</span> from{" "}
            {m.duplicate_of.from} ({formatLongDate(m.duplicate_of.created_at)}, {formatTime(m.duplicate_of.created_at)}). Merged, it keeps the recording and takes the
            better transcript and notes.
          </p>
        ) : (
          <p className="leading-relaxed text-ink-soft">
            <span className="font-medium text-ink">Merged with the copy from {last.from}.</span> Its transcript and notes
            are used{moved(last.offset)}; the recording, your flags and notes stay. The earlier transcript is in
            Transcript → Versions.
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {busy && <Spinner className="h-4 w-4" />}
        {m.duplicate_of ? (
          <>
            <button
              className="btn btn-sm btn-ghost"
              disabled={busy}
              onClick={() => void run(() => api.keepApart(m.id, m.duplicate_of!.id))}
            >
              Not the same
            </button>
            <button
              className="btn btn-sm btn-primary"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  const r = await api.mergeDuplicates([[m.id, m.duplicate_of!.id]]);
                  if (r.problems.length) throw new Error(r.problems[0]);
                  if (r.kept[0] && r.kept[0] !== m.id) onOpenMeeting(r.kept[0]);
                })
              }
            >
              Merge
            </button>
          </>
        ) : (
          <button className="btn btn-sm btn-ghost" disabled={busy} onClick={() => void run(() => api.unmerge(m.id))}>
            <RefreshIcon size={14} />
            Undo merge
          </button>
        )}
      </div>
    </div>
  );
}

/** Settings: look through the library for meetings that are there twice, and merge them. */
export function FindDuplicates({ onChanged }: { onChanged: () => void }) {
  const [pairs, setPairs] = useState<DuplicatePair[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const find = async () => {
    setBusy(true);
    setNote(null);
    try {
      setPairs((await api.duplicates()).pairs);
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const mergeAll = async () => {
    if (!pairs) return;
    setBusy(true);
    try {
      const r = await api.mergeDuplicates(pairs.map((p) => [p.keep.id, p.other.id]));
      setNote(
        `Merged ${r.merged} meeting${r.merged === 1 ? "" : "s"}. Each one can be undone from the meeting.` +
          (r.problems.length ? ` Skipped ${r.problems.length}: ${r.problems[0]}` : ""),
      );
      setPairs(null);
      onChanged();
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="py-3.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-label font-medium">Find meetings that are here twice</div>
          <div className="mt-0.5 text-hint text-ink-soft">
            Looks through every meeting for one Trailmix recorded while Granola was taking notes too.
          </div>
        </div>
        <button className="btn btn-sm btn-soft" disabled={busy} onClick={() => void find()}>
          {busy && !pairs ? <Spinner className="h-4 w-4" /> : null}
          Find duplicates
        </button>
      </div>
      {pairs && (
        <div className="mt-3 rounded-md border border-line">
          {pairs.length === 0 ? (
            <p className="px-3 py-3 text-hint text-ink-soft">None found.</p>
          ) : (
            <>
              <ul className="divide-y divide-line">
                {pairs.map((p) => (
                  <li key={`${p.keep.id}-${p.other.id}`} className="px-3 py-2.5 text-hint">
                    <div className="font-medium text-ink">{p.keep.title}</div>
                    <div className="mt-0.5 text-ink-soft">
                      {formatLongDate(p.keep.created_at)}, {formatTime(p.keep.created_at)} · {p.keep.label}
                      {p.keep.has_audio ? " (with the recording)" : ""} and {p.other.label} “{p.other.title}” ·{" "}
                      {Math.round(p.score * 100)}% the same words
                    </div>
                  </li>
                ))}
              </ul>
              <div className="flex items-center justify-between gap-3 border-t border-line px-3 py-2.5">
                <span className="text-hint text-ink-soft">
                  Keeps each recording; uses the {pairs.some((p) => p.other.source === "granola") ? "Granola" : "better"}{" "}
                  transcript and notes.
                </span>
                <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => void mergeAll()}>
                  {busy && <Spinner className="h-4 w-4" />}
                  Merge {pairs.length}
                </button>
              </div>
            </>
          )}
        </div>
      )}
      {note && <p className="mt-2 text-hint text-ink-soft">{note}</p>}
    </div>
  );
}
