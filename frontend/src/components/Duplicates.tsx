import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { DuplicatePair, Meeting, MergedCopy, MergeToReview } from "../api";
import { formatLongDate, formatTime } from "../format";
import { ArrowRightIcon, CheckIcon, ColumnsIcon, RefreshIcon } from "./icons";
import { Spinner } from "./ui";

function moved(seconds: number) {
  const s = Math.round(Math.abs(seconds));
  if (s < 2) return "";
  const text = s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `${s} s`;
  return `, moved ${text} to line up with the recording`;
}

/** What a meeting took from the copy merged into it, in a sentence. */
export function tookFrom(r: MergedCopy): string {
  const theirs = r.from === "Trailmix" ? "Trailmix's" : r.from.startsWith("a ") ? `the ${r.from.slice(2)}'s` : `${r.from}'s`;
  const parts = [r.took.transcript && "transcript", r.took.notes && "notes"].filter(Boolean);
  let text = parts.length
    ? `Uses ${theirs} ${parts.join(" and ")}${parts.length === 1 ? `, keeps its own ${r.took.transcript ? "notes" : "transcript"}` : ""}`
    : `Keeps its own transcript and notes (${theirs} transcript is in Versions)`;
  if (r.took.title) text += `, and the title from ${r.from} (was “${r.title_before}”)`;
  return text + moved(r.offset) + ".";
}

/** "Granola's “Weekly sync” · Oct 26, 4:30 PM · 30 min": the copy that was merged in. */
function CopyLine({ r }: { r: MergedCopy }) {
  return (
    <>
      <span className="font-medium text-ink">
        {r.from === "Trailmix" ? "A Trailmix recording" : `${r.from[0].toUpperCase()}${r.from.slice(1)}`}: “{r.title}”
      </span>{" "}
      <span className="whitespace-nowrap tabular-nums">
        · {formatShortDate(r.created_at)}, {formatTime(r.created_at)}
        {r.minutes ? ` · ${r.minutes} min` : ""}
      </span>
    </>
  );
}

const formatShortDate = (iso: string) => {
  const d = new Date(iso);
  const year = d.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year });
};

/**
 * On a meeting: a copy of it that was merged in by itself, waiting for you to keep it or undo it (once kept, a quiet
 * line with Undo stays); or that it looks like the same meeting as another one (Merge / Not the same), when you'd
 * rather be asked.
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
  const waiting = m.merged.filter((r) => !r.reviewed);
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
  const undo = () =>
    void run(async () => {
      const r = await api.unmerge(m.id);
      onOpenMeeting(r.id); // (onChanged reloads this one; the copy that came back opens)
    });

  if (!m.duplicate_of && !waiting.length) {
    return (
      <p className="-mt-2 mb-6 flex flex-wrap items-center gap-x-2 text-hint text-ink-soft">
        <ColumnsIcon size={14} className="text-ink-faint" />
        Merged with the copy from {last.from} ({formatShortDate(last.at)}).
        <button className="font-medium text-forest underline-offset-2 hover:underline" disabled={busy} onClick={undo}>
          Undo merge
        </button>
        {busy && <Spinner className="h-3.5 w-3.5" />}
      </p>
    );
  }

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
          <div className="min-w-0 leading-relaxed text-ink-soft">
            <p className="font-medium text-ink">
              Merged automatically: {waiting.length === 1 ? "a copy" : `${waiting.length} copies`} of this meeting
            </p>
            {waiting.map((r) => (
              <p key={r.at} className="mt-0.5">
                <CopyLine r={r} />
                <br />
                {tookFrom(r)}
              </p>
            ))}
          </div>
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
          <>
            <button className="btn btn-sm btn-ghost" disabled={busy} onClick={undo} data-tip="Split it back into two meetings">
              <RefreshIcon size={14} />
              Undo merge
            </button>
            <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => void run(() => api.keepMerged(m.id))}>
              <CheckIcon size={14} />
              Keep merged
            </button>
          </>
        )}
      </div>
    </div>
  );
}

/**
 * Import page: every merge Trailmix made by itself that you haven't looked at yet, what went into what, each one
 * kept (it stops asking) or undone (the two meetings are apart again, and not merged again).
 */
export function MergeReview({
  onOpenMeeting,
  onChanged,
  onError,
}: {
  onOpenMeeting: (id: number) => void;
  onChanged: () => void;
  onError: (msg: string) => void;
}) {
  const [items, setItems] = useState<MergeToReview[] | null>(null);
  const [busy, setBusy] = useState<number | "all" | null>(null);

  const load = useCallback(() => {
    api
      .mergesToReview()
      .then((r) => setItems(r.meetings))
      .catch(() => setItems([]));
  }, []);
  useEffect(load, [load]);

  if (!items?.length) return null;

  const run = async (which: number | "all", fn: () => Promise<unknown>) => {
    setBusy(which);
    try {
      await fn();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(null);
      load();
      onChanged();
    }
  };

  return (
    <section className="panel animate-enter p-5 sm:p-7" aria-labelledby="merges-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id="merges-heading" className="font-display text-lead font-semibold">
            Merged automatically
          </h2>
          <p className="mt-1 text-label leading-relaxed text-ink-soft">
            These came in twice, from Trailmix and another app, and were merged into one meeting. Keep each merge, or
            undo it to have the two meetings back.
          </p>
        </div>
        {items.length > 1 && (
          <button
            className="btn btn-sm btn-soft"
            disabled={busy !== null}
            onClick={() =>
              void run("all", async () => {
                for (const m of items) await api.keepMerged(m.id);
              })
            }
          >
            {busy === "all" ? <Spinner className="h-4 w-4" /> : <CheckIcon size={14} />}
            Keep all {items.length}
          </button>
        )}
      </div>

      <ul className="mt-4 divide-y divide-line rounded-md border border-line" aria-label="Merges to review">
        {items.map((m) => {
          const waiting = m.merged.filter((r) => !r.reviewed);
          const canUndo = waiting.some((r) => r.last);
          return (
            <li key={m.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-hint">
              <div className="min-w-0 flex-1 basis-80 leading-relaxed text-ink-soft">
                {waiting.map((r) => (
                  <div key={r.at}>
                    <CopyLine r={r} />
                  </div>
                ))}
                <div className="flex min-w-0 items-baseline gap-1.5">
                  <ArrowRightIcon size={12} className="shrink-0 translate-y-px text-ink-faint" />
                  <span className="min-w-0">
                    merged into{" "}
                    <button className="text-left font-medium text-forest underline-offset-2 hover:underline" onClick={() => onOpenMeeting(m.id)}>
                      “{m.title}”
                    </button>{" "}
                    <span className="whitespace-nowrap">
                      ({m.has_audio ? `the ${m.label} recording` : m.label}
                      {m.minutes ? `, ${m.minutes} min` : ""})
                    </span>
                  </span>
                </div>
                {waiting.map((r) => (
                  <div key={r.at} className="mt-0.5 text-ink-faint">
                    {tookFrom(r)}
                    {r.score ? ` ${Math.round(r.score * 100)}% the same words.` : ""}
                  </div>
                ))}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {busy === m.id && <Spinner className="h-4 w-4" />}
                {canUndo && (
                  <button
                    className="btn btn-sm btn-ghost"
                    disabled={busy !== null}
                    onClick={() => void run(m.id, () => api.unmerge(m.id))}
                    data-tip="Split it back into two meetings"
                  >
                    <RefreshIcon size={14} />
                    Undo
                  </button>
                )}
                <button
                  className="btn btn-sm btn-primary"
                  disabled={busy !== null}
                  onClick={() => void run(m.id, () => api.keepMerged(m.id))}
                >
                  <CheckIcon size={14} />
                  Keep
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
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
