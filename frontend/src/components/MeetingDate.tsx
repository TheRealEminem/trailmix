import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import type { DateGuess, Meeting } from "../api";
import { formatDuration, formatLongDate, formatTime } from "../format";
import { CalendarIcon, ClockIcon, PencilIcon, SparkleIcon } from "./icons";
import { Spinner } from "./ui";

const pad = (n: number) => String(n).padStart(2, "0");
const dayOf = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const timeOf = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

function readable(day: string, sure: DateGuess["sure_of"]): string {
  const d = new Date(`${day}T12:00`);
  if (sure === "year") return String(d.getFullYear());
  if (sure === "month") return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
}

/** The meeting's date and time in the header; click to change them, or to have the AI work out the date from
 * what was said (for a transcript imported without one). */
export default function MeetingDate({
  meeting: m,
  onSave,
}: {
  meeting: Meeting;
  onSave: (iso: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="group -mx-1.5 inline-flex items-center gap-x-4 gap-y-1.5 rounded-md px-1.5 py-0.5 transition-colors hover:bg-forest/[.05] hover:text-ink"
        data-tip="Change the date"
      >
        <span className="inline-flex items-center gap-1.5">
          <CalendarIcon size={16} className="text-ink-faint" />
          {formatLongDate(m.created_at)}
        </span>
        <span className="inline-flex items-center gap-1.5 tabular-nums">
          <ClockIcon size={16} className="text-ink-faint" />
          {formatTime(m.created_at)} · {formatDuration(m.duration_sec)}
        </span>
        <PencilIcon size={14} className="-ml-2.5 text-ink-faint opacity-0 transition-opacity group-hover:opacity-100" />
      </button>
      {open && <DateDialog meeting={m} onSave={onSave} onClose={() => setOpen(false)} />}
    </>
  );
}

function DateDialog({
  meeting: m,
  onSave,
  onClose,
}: {
  meeting: Meeting;
  onSave: (iso: string) => Promise<void>;
  onClose: () => void;
}) {
  const now = new Date(m.created_at);
  const [day, setDay] = useState(dayOf(now));
  const [time, setTime] = useState(timeOf(now));
  const [guess, setGuess] = useState<DateGuess | null>(null);
  const [asking, setAsking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const ask = async () => {
    setAsking(true);
    setError("");
    try {
      setGuess(await api.guessDate(m.id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setAsking(false);
    }
  };

  const save = async () => {
    const when = new Date(`${day}T${time || "00:00"}`);
    if (Number.isNaN(when.getTime())) {
      setError("Pick a date first");
      return;
    }
    setSaving(true);
    try {
      await onSave(when.toISOString());
      onClose();
    } catch (e) {
      setError((e as Error).message);
      setSaving(false);
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex animate-fade-in items-center justify-center bg-[var(--scrim)] p-5 backdrop-blur-[3px]"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="date-title"
        className="w-full max-w-[420px] animate-dialog-in rounded-xl border border-line bg-surface p-6 shadow-lg"
      >
        <div className="well well-forest mb-4 h-10 w-10 rounded-md">
          <CalendarIcon size={20} />
        </div>
        <h2 id="date-title" className="text-lead font-semibold tracking-[-0.01em]">
          When was this meeting?
        </h2>
        <p className="mt-1.5 text-label leading-relaxed text-ink-soft">
          It moves to that day in your meeting list, and any exported copy follows.
        </p>

        <div className="mt-4 flex gap-2.5">
          <input
            type="date"
            className="field min-w-0 flex-1"
            value={day}
            max={dayOf(new Date())}
            onChange={(e) => setDay(e.target.value)}
            aria-label="Date"
          />
          <input
            type="time"
            className="field w-32"
            value={time}
            onChange={(e) => setTime(e.target.value)}
            aria-label="Start time"
          />
        </div>

        {m.transcript && (
          <div className="mt-4 rounded-lg border border-line bg-forest/[.03] p-3.5">
            {!guess ? (
              <div className="flex items-center justify-between gap-3">
                <p className="text-hint leading-relaxed text-ink-soft">
                  Not sure? The AI can look for clues in what was said.
                </p>
                <button type="button" className="btn btn-sm btn-ghost shrink-0" onClick={() => void ask()} disabled={asking}>
                  {asking ? <Spinner /> : <SparkleIcon size={15} className="text-sun-deep" />}
                  {asking ? "Reading…" : "Find the date"}
                </button>
              </div>
            ) : guess.date ? (
              <div>
                <p className="text-label font-medium">
                  Probably {readable(guess.date, guess.sure_of)}
                </p>
                {guess.why && <p className="mt-1 text-hint leading-relaxed text-ink-soft">{guess.why}</p>}
                {guess.sure_of !== "day" && (
                  <p className="mt-1 text-hint leading-relaxed text-ink-faint">
                    Only the {guess.sure_of} is clear, so check the day before saving.
                  </p>
                )}
                <button
                  type="button"
                  className="btn btn-sm btn-ghost mt-2.5"
                  onClick={() => setDay(guess.date!)}
                  disabled={day === guess.date}
                >
                  {day === guess.date ? "Filled in above" : "Use this date"}
                </button>
              </div>
            ) : (
              <p className="text-hint leading-relaxed text-ink-soft">
                Couldn't tell: {guess.why.replace(/\.$/, "")}. Pick the date yourself.
              </p>
            )}
          </div>
        )}

        {error && <p className="mt-3 text-hint text-trail-deep">{error}</p>}

        <div className="mt-6 flex justify-end gap-2">
          <button className="btn btn-md btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-md btn-primary" onClick={() => void save()} disabled={saving || !day}>
            {saving ? <Spinner /> : null}
            Save
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
