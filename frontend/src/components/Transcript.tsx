import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, RefObject } from "react";
import { api } from "../api";
import type { Bookmark, Health, Meeting, Segment } from "../api";
import { formatDuration } from "../format";
import { CloseIcon, FlagIcon, LinesIcon, PauseIcon, PencilIcon, PlayIcon } from "./icons";
import { ContourBadge } from "./illustrations";
import { CopyButton, SpeakerChip } from "./ui";

interface Props {
  meeting: Meeting;
  health: Health | null;
  pending: boolean;
  onBookmarks: (b: Bookmark[]) => void;
  onSpeakers: (names: Record<string, string>) => void;
}

const SPEEDS = [1, 1.25, 1.5, 2];

/** Inline rename for a speaker label ("Them" -> "Dana"). */
function SpeakerName({ label, name, onSave }: { label: string; name: string; onSave: (v: string) => void }) {
  const [editing, setEditing] = useState(false);
  if (editing) {
    return (
      <input
        autoFocus
        defaultValue={name === label ? "" : name}
        placeholder={label}
        onBlur={(e) => {
          setEditing(false);
          if (e.target.value.trim() !== name) onSave(e.target.value.trim());
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") setEditing(false);
        }}
        className="field h-6 w-28 px-2 py-0 text-meta"
        aria-label={`Name for ${label}`}
      />
    );
  }
  return (
    <button onClick={() => setEditing(true)} className="group inline-flex items-center gap-1 rounded-full" data-tip={`Rename ${label}`}>
      <SpeakerChip speaker={label} name={name} />
      <PencilIcon size={16} className="text-ink-faint opacity-0 transition-opacity group-hover:opacity-100" />
    </button>
  );
}

type Turn = { speaker: string | null; start: number; end: number; lines: { s: Segment; i: number }[] };

/** Lines become turns: the same speaker, without a pause longer than 2 s, up to about a minute per paragraph. */
function toTurns(segments: Segment[]): Turn[] {
  const turns: Turn[] = [];
  segments.forEach((s, i) => {
    const last = turns[turns.length - 1];
    if (last && last.speaker === s.speaker && s.start - last.end <= 2 && s.end - last.start <= 60) {
      last.lines.push({ s, i });
      last.end = Math.max(last.end, s.end);
    } else {
      turns.push({ speaker: s.speaker, start: s.start, end: s.end, lines: [{ s, i }] });
    }
  });
  return turns;
}

const within = (b: Bookmark, start: number, end: number) => b.t >= start - 0.5 && b.t <= end + 0.5;

/** The line playing at `time` (the last one started by then), or -1. */
function lineAt(segments: Segment[], time: number): number {
  let lo = 0;
  let hi = segments.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (segments[mid].start <= time + 0.05) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/**
 * The player. It keeps the playback time to itself (it changes four times a second) and tells the transcript only
 * when the line playing changes, so the transcript draws again once a line, not on every tick.
 */
function PlayerBar({
  audio,
  meetingId,
  duration,
  segments,
  bookmarks,
  onPlaying,
  onLine,
  onError,
}: {
  audio: RefObject<HTMLAudioElement>;
  meetingId: number;
  duration: number;
  segments: Segment[];
  bookmarks: Bookmark[];
  onPlaying: (playing: boolean) => void;
  onLine: (line: number) => void;
  onError: () => void;
}) {
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [length, setLength] = useState(duration);
  const [speed, setSpeed] = useState(1);
  const line = playing || time > 0 ? lineAt(segments, time) : -1;
  useEffect(() => onLine(line), [line, onLine]);
  useEffect(() => onPlaying(playing), [playing, onPlaying]);

  const toggle = () => {
    const a = audio.current;
    if (!a) return;
    if (a.paused) void a.play();
    else a.pause();
  };

  return (
    <div className="glass sticky top-[4.25rem] z-10 mb-6 flex items-center gap-3 rounded-lg px-2.5 py-2 lg:top-3">
      <audio
        ref={audio}
        src={api.audioUrl(meetingId)}
        // Nothing loads until Play: on Linux, WebKitGTK's media player starting on the recording as the
        // tab opened froze the whole window (issue #14). The length shown until then is the meeting's own.
        preload="none"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
        onLoadedMetadata={(e) => Number.isFinite(e.currentTarget.duration) && setLength(e.currentTarget.duration)}
        onError={onError}
      />
      <button onClick={toggle} className="btn btn-primary h-9 w-9 shrink-0 rounded-full p-0" aria-label={playing ? "Pause" : "Play"}>
        {playing ? <PauseIcon size={16} /> : <PlayIcon size={16} className="translate-x-px" />}
      </button>
      <span className="hidden w-[6.5rem] shrink-0 font-mono text-meta tabular-nums text-ink-soft sm:block">
        {formatDuration(time)} / {formatDuration(length)}
      </span>
      <div className="relative flex-1">
        <input
          type="range"
          min={0}
          max={length || 1}
          step={0.1}
          value={time}
          onChange={(e) => {
            const t = Number(e.target.value);
            setTime(t);
            if (audio.current) audio.current.currentTime = t;
          }}
          className="range block"
          style={{ "--pct": `${(time / (length || 1)) * 100}%` } as CSSProperties}
          aria-label="Position"
        />
        {bookmarks.map((b, i) => (
          <span
            key={i}
            className="pointer-events-none absolute top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-sun ring-2 ring-[color:var(--surface-solid)]"
            style={{ left: `${(b.t / (length || 1)) * 100}%` }}
          />
        ))}
      </div>
      <button
        onClick={() => {
          const next = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length];
          setSpeed(next);
          if (audio.current) audio.current.playbackRate = next;
        }}
        className="btn btn-sm btn-ghost w-14 shrink-0 font-mono tabular-nums"
        data-tip="Playback speed"
      >
        {speed}×
      </button>
    </div>
  );
}

// Off screen, a turn is laid out at about this height and not drawn (content-visibility), so a long transcript
// costs what's on screen. Find in page still finds text in it.
const OFFSCREEN: CSSProperties = { contentVisibility: "auto", containIntrinsicSize: "auto 72px" };

/** One speaker's turn. Drawn again only when its own lines, flags or highlighted line change. */
const TurnRow = memo(function TurnRow({
  turn,
  name,
  playingLine,
  bookmarks,
  hasAudio,
  onPlay,
  onMark,
  rowRefs,
}: {
  turn: Turn;
  name: string | undefined;
  /** The line playing, when it's in this turn; else -1. */
  playingLine: number;
  bookmarks: Bookmark[];
  hasAudio: boolean;
  onPlay: (t: number) => void;
  onMark: (start: number, end: number) => void;
  rowRefs: RefObject<(HTMLElement | null)[]>;
}) {
  const marked = bookmarks.some((b) => within(b, turn.start, turn.end));
  const live = playingLine >= 0;
  return (
    <div style={OFFSCREEN} className={turn.speaker ? "grid grid-cols-1 gap-y-1 sm:grid-cols-[5.5rem_1fr] sm:gap-x-3" : ""}>
      {turn.speaker && (
        <div className="sm:pt-[7px]">
          <SpeakerChip speaker={turn.speaker} name={name} />
        </div>
      )}
      <div className={`group -mx-2 flex gap-2.5 rounded-md px-2 py-1 transition-colors duration-150 ${live ? "bg-sun-soft/40" : ""}`}>
        <button
          onClick={() => hasAudio && onPlay(turn.start)}
          disabled={!hasAudio}
          className="w-11 shrink-0 self-start pt-[5px] text-right font-mono text-meta tabular-nums text-ink-soft transition-colors enabled:hover:text-forest"
          aria-label={hasAudio ? `Play from ${formatDuration(turn.start)}` : undefined}
        >
          {formatDuration(turn.start)}
        </button>
        <p className="min-w-0 flex-1 text-body leading-[1.7]">
          {turn.lines.map(({ s, i }) => (
            <span
              key={i}
              ref={(el) => {
                if (rowRefs.current) rowRefs.current[i] = el;
              }}
              onClick={() => hasAudio && onPlay(s.start)}
              className={`-mx-0.5 rounded-sm px-0.5 transition-colors duration-150 ${
                i === playingLine ? "bg-sun-soft" : hasAudio ? "cursor-pointer hover:bg-forest/[.06]" : ""
              } ${bookmarks.some((b) => within(b, s.start, s.end)) ? "underline decoration-sun decoration-2 underline-offset-4" : ""}`}
            >
              {s.text}{" "}
            </span>
          ))}
        </p>
        <button
          onClick={() => onMark(turn.start, turn.end)}
          className={`icon-btn h-7 w-7 self-start transition-opacity focus-visible:opacity-100 ${
            marked ? "text-sun-deep opacity-100" : "text-ink-faint opacity-0 hover:!text-sun-deep group-hover:opacity-100 [@media(pointer:coarse)]:opacity-50"
          }`}
          aria-label={marked ? "Remove flag" : "Flag this moment"}
          data-tip={marked ? "Remove flag" : "Flag this moment"}
        >
          <FlagIcon size={16} fill={marked ? "currentColor" : "none"} />
        </button>
      </div>
    </div>
  );
});

/** How long following the playing line waits after you've scrolled yourself. */
const FOLLOW_PAUSE_MS = 4000;

export default function Transcript({ meeting: m, pending, onBookmarks, onSpeakers }: Props) {
  const audio = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(-1);
  const [audioError, setAudioError] = useState(false);
  const rowRefs = useRef<(HTMLElement | null)[]>([]);

  const segments: Segment[] = useMemo(() => m.segments ?? [], [m.segments]);
  const names = m.speaker_names;
  // Who spoke: the labels in the transcript ("You", "Them", or "Speaker 1"...).
  const speakers = useMemo(() => {
    const found = [...new Set(segments.map((s) => s.speaker).filter((x): x is string => !!x))];
    if (!found.length && m.has_system) found.push("You", "Them");
    return found;
  }, [segments, m.has_system]);
  const turns = useMemo(() => toTurns(segments), [segments]);
  const hasAudio = !m.audio_deleted && m.audio_bytes > 0 && !audioError;

  // Follows the line playing, unless you've just scrolled yourself (wheel, touch or keys: following scrolls too,
  // so the scroll event itself can't tell).
  const scrolledAt = useRef(0);
  useEffect(() => {
    const mine = () => (scrolledAt.current = Date.now());
    const keys = (e: KeyboardEvent) => ["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown", " "].includes(e.key) && mine();
    window.addEventListener("wheel", mine, { passive: true });
    window.addEventListener("touchmove", mine, { passive: true });
    window.addEventListener("keydown", keys);
    return () => {
      window.removeEventListener("wheel", mine);
      window.removeEventListener("touchmove", mine);
      window.removeEventListener("keydown", keys);
    };
  }, []);
  useEffect(() => {
    if (!playing || current < 0 || Date.now() - scrolledAt.current < FOLLOW_PAUSE_MS) return;
    rowRefs.current[current]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [current, playing]);

  const playFrom = useCallback((t: number) => {
    const a = audio.current;
    if (!a) return;
    a.currentTime = Math.max(0, t - 0.3);
    void a.play();
  }, []);

  // The latest bookmarks and handler, for the rows' steady callback.
  const latest = useRef({ bookmarks: m.bookmarks, onBookmarks });
  latest.current = { bookmarks: m.bookmarks, onBookmarks };
  const markTurn = useCallback((start: number, end: number) => {
    const { bookmarks, onBookmarks: save } = latest.current;
    const inside = bookmarks.filter((b) => within(b, start, end));
    save(inside.length ? bookmarks.filter((b) => !inside.includes(b)) : [...bookmarks, { t: start, note: "" }]);
  }, []);

  if (!m.transcript) {
    return (
      <div className="py-8 text-center">
        <ContourBadge>
          <LinesIcon size={18} />
        </ContourBadge>
        <p className="mt-3 font-medium">No transcript yet</p>
        <p className="mx-auto mt-1 max-w-sm text-label leading-relaxed text-ink-soft">
          {pending ? "The full-quality transcript will appear here when processing finishes." : "Nothing was transcribed for this meeting."}
        </p>
      </div>
    );
  }

  return (
    <div data-private="transcript">
      {/* toolbar */}
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <span className="text-hint text-ink-soft">
          {m.transcribed_with ? `Transcribed with ${m.transcribed_with}` : ""}
        </span>
        <CopyButton text={m.transcript} />
      </div>

      <>
          {/* speakers & moments */}
          <div className="inset-surface mb-6 space-y-3 px-4 py-3.5 text-hint text-ink-soft">
            <div className="flex flex-wrap items-center gap-2">
              <span className="eyebrow">Speakers</span>
              {speakers.length ? (
                <>
                  {speakers.map((label) => (
                    <SpeakerName
                      key={label}
                      label={label}
                      name={names[label] ?? label}
                      onSave={(v) => onSpeakers({ ...names, [label]: v })}
                    />
                  ))}
                  <span>Click a name to change it.</span>
                </>
              ) : (
                <span>One audio source, so speakers aren't labeled.</span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="eyebrow mr-0.5">Moments</span>
              {m.bookmarks.length === 0 && <span>None flagged. Hover a line and click the flag to mark it.</span>}
              {m.bookmarks.map((b, i) => (
                <span key={i} className="chip gap-0.5 bg-sun-soft pr-0.5 text-sun-deep">
                  <button onClick={() => playFrom(b.t)} className="inline-flex items-center gap-1 tabular-nums" data-tip={b.note || "Play from here"}>
                    <FlagIcon size={12} /> {formatDuration(b.t)}
                  </button>
                  <button
                    onClick={() => onBookmarks(m.bookmarks.filter((x) => x !== b))}
                    className="rounded-full p-0.5 opacity-60 transition-opacity hover:opacity-100"
                    aria-label={`Remove moment at ${formatDuration(b.t)}`}
                  >
                    <CloseIcon size={12} />
                  </button>
                </span>
              ))}
            </div>
          </div>

          {/* player */}
          {hasAudio && (
            <PlayerBar
              audio={audio}
              meetingId={m.id}
              duration={m.duration_sec}
              segments={segments}
              bookmarks={m.bookmarks}
              onPlaying={setPlaying}
              onLine={setCurrent}
              onError={() => setAudioError(true)}
            />
          )}

          {turns.length === 0 ? (
            <pre className="whitespace-pre-wrap font-sans text-body leading-[1.7]">{m.transcript}</pre>
          ) : (
            <div className="space-y-3">
              {turns.map((turn, ti) => (
                <TurnRow
                  key={ti}
                  turn={turn}
                  name={turn.speaker ? names[turn.speaker] : undefined}
                  playingLine={turn.lines.some(({ i }) => i === current) ? current : -1}
                  bookmarks={m.bookmarks}
                  hasAudio={hasAudio}
                  onPlay={playFrom}
                  onMark={markTurn}
                  rowRefs={rowRefs}
                />
              ))}
            </div>
          )}
      </>
    </div>
  );
}
