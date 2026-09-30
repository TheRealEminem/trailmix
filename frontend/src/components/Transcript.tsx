import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { api } from "../api";
import type { Bookmark, Health, Meeting, Segment } from "../api";
import { formatDuration } from "../format";
import Compare from "./Compare";
import { CloseIcon, ColumnsIcon, FlagIcon, LinesIcon, PauseIcon, PencilIcon, PlayIcon } from "./icons";
import { ContourBadge } from "./illustrations";
import { CopyButton, Segmented, SpeakerChip } from "./ui";

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

export default function Transcript({ meeting: m, health, pending, onBookmarks, onSpeakers }: Props) {
  const [view, setView] = useState<"final" | "compare">("final");
  const audio = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [length, setLength] = useState(m.duration_sec);
  const [speed, setSpeed] = useState(1);
  const [audioError, setAudioError] = useState(false);
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);

  const segments: Segment[] = m.segments ?? [];
  const names = m.speaker_names;
  const hasAudio = !m.audio_deleted && m.audio_bytes > 0 && !audioError;
  const hasCompare = !!m.draft?.length && segments.length > 0;
  const current = playing || time > 0 ? segments.findLastIndex((s) => s.start <= time + 0.05) : -1;

  useEffect(() => {
    if (playing && current >= 0) rowRefs.current[current]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [current, playing]);

  const playFrom = (t: number) => {
    const a = audio.current;
    if (!a) return;
    a.currentTime = Math.max(0, t - 0.3);
    void a.play();
  };
  const toggle = () => {
    const a = audio.current;
    if (!a) return;
    if (a.paused) void a.play();
    else a.pause();
  };

  const markAt = (s: Segment) => {
    const existing = m.bookmarks.find((b) => b.t >= s.start - 0.5 && b.t <= s.end + 0.5);
    onBookmarks(existing ? m.bookmarks.filter((b) => b !== existing) : [...m.bookmarks, { t: s.start, note: "" }]);
  };
  const isMarked = (s: Segment) => m.bookmarks.some((b) => b.t >= s.start - 0.5 && b.t <= s.end + 0.5);

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

  // Consecutive lines from the same speaker share one label.
  const turns: { speaker: string | null; lines: { s: Segment; i: number }[] }[] = [];
  segments.forEach((s, i) => {
    const last = turns[turns.length - 1];
    if (last && last.speaker === s.speaker) last.lines.push({ s, i });
    else turns.push({ speaker: s.speaker, lines: [{ s, i }] });
  });

  return (
    <div>
      {/* toolbar */}
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        {hasCompare ? (
          <Segmented
            kind="tab"
            size="sm"
            label="Transcript view"
            value={view}
            onChange={setView}
            options={[
              { id: "final", label: "Final transcript", icon: <LinesIcon size={16} /> },
              { id: "compare", label: "Draft vs final", icon: <ColumnsIcon size={16} /> },
            ]}
          />
        ) : (
          <span />
        )}
        <CopyButton text={m.transcript} />
      </div>

      {view === "compare" && hasCompare ? (
        <Compare
          draft={m.draft!}
          segments={segments}
          names={names}
          liveModel={health?.transcription.live_model ?? "live model"}
          finalModel={health?.transcription.model ?? "final model"}
        />
      ) : (
        <>
          {/* speakers & moments */}
          <div className="inset-surface mb-6 space-y-3 px-4 py-3.5 text-hint text-ink-soft">
            <div className="flex flex-wrap items-center gap-2">
              <span className="eyebrow">Speakers</span>
              {m.has_system ? (
                <>
                  <SpeakerName label="You" name={names.You} onSave={(v) => onSpeakers({ ...names, You: v })} />
                  <SpeakerName label="Them" name={names.Them} onSave={(v) => onSpeakers({ ...names, Them: v })} />
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
            <div className="glass sticky top-[4.25rem] z-10 mb-6 flex items-center gap-3 rounded-lg px-2.5 py-2 lg:top-3">
              <audio
                ref={audio}
                src={api.audioUrl(m.id)}
                preload="metadata"
                onPlay={() => setPlaying(true)}
                onPause={() => setPlaying(false)}
                onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
                onLoadedMetadata={(e) => Number.isFinite(e.currentTarget.duration) && setLength(e.currentTarget.duration)}
                onError={() => setAudioError(true)}
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
                {m.bookmarks.map((b, i) => (
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
          )}

          {turns.length === 0 ? (
            <pre className="whitespace-pre-wrap font-sans text-body leading-[1.7]">{m.transcript}</pre>
          ) : (
            <div className="space-y-5">
              {turns.map((turn, ti) => (
                <div key={ti} className={turn.speaker ? "grid grid-cols-1 gap-y-1 sm:grid-cols-[5.5rem_1fr] sm:gap-x-3" : ""}>
                  {turn.speaker && (
                    <div className="sm:pt-[7px]">
                      <SpeakerChip speaker={turn.speaker} name={names[turn.speaker]} />
                    </div>
                  )}
                  <div className="space-y-px">
                    {turn.lines.map(({ s, i }) => {
                      const active = i === current;
                      const marked = isMarked(s);
                      return (
                        <div
                          key={i}
                          ref={(el) => {
                            rowRefs.current[i] = el;
                          }}
                          className={`group -mx-2 flex gap-2.5 rounded-md px-2 py-1 transition-colors duration-150 ${
                            active ? "bg-sun-soft/80" : hasAudio ? "hover:bg-forest/[.04]" : ""
                          }`}
                        >
                          <button
                            onClick={() => hasAudio && playFrom(s.start)}
                            disabled={!hasAudio}
                            className="w-11 shrink-0 self-start pt-[5px] text-right font-mono text-meta tabular-nums text-ink-soft transition-colors enabled:hover:text-forest"
                            aria-label={hasAudio ? `Play from ${formatDuration(s.start)}` : undefined}
                          >
                            {formatDuration(s.start)}
                          </button>
                          <p
                            onClick={() => hasAudio && playFrom(s.start)}
                            className={`min-w-0 flex-1 text-body leading-[1.65] ${hasAudio ? "cursor-pointer" : ""}`}
                          >
                            {s.text}
                          </p>
                          <button
                            onClick={() => markAt(s)}
                            className={`icon-btn h-7 w-7 self-start transition-opacity focus-visible:opacity-100 ${
                              marked ? "text-sun-deep opacity-100" : "text-ink-faint opacity-0 hover:!text-sun-deep group-hover:opacity-100 [@media(pointer:coarse)]:opacity-50"
                            }`}
                            aria-label={marked ? "Remove flag" : "Flag this moment"}
                            data-tip={marked ? "Remove flag" : "Flag this moment"}
                          >
                            <FlagIcon size={16} fill={marked ? "currentColor" : "none"} />
                          </button>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
