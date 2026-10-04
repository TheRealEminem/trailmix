import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import type {
  Bookmark,
  DraftLine,
  Health,
  LiveRecording,
  NativeRecorder,
} from "../api";
import type { ActiveRecording } from "../capture";
import { formatDuration, formatLongDate, shortModel } from "../format";
import {
  AlertIcon,
  FlagIcon,
  LinesIcon,
  MicIcon,
  MonitorIcon,
  StopIcon,
} from "./icons";
import { TrailScene } from "./illustrations";
import LevelMeters from "./LevelMeter";
import MicPicker from "./MicPicker";
import SoundCheck from "./SoundCheck";
import { ControlRow, Pulse, SpeakerChip, Spinner, SwitchRow } from "./ui";

interface Props {
  rec: ActiveRecording | null;
  /** A recording the menu bar helper is capturing; shown live here, with Mark and Stop. */
  remote: LiveRecording | null;
  /** The menu bar recorder, if it's running: then recording goes through it, with its own settings. */
  native: NativeRecorder | null;
  elapsed: number;
  starting: boolean;
  stopping: boolean;
  micDeviceId: string;
  onMicChange: (id: string) => void;
  includeMeetingAudio: boolean;
  onIncludeMeetingAudio: (v: boolean) => void;
  liveDraft: boolean;
  onLiveDraft: (v: boolean) => void;
  notice: string | null;
  drafts: DraftLine[];
  health: Health | null;
  backendDown: boolean;
  bookmarks: Bookmark[];
  dusk: boolean;
  onStart: () => void;
  onStop: () => void;
  onMark: () => void;
}

const delay = (ms: number): CSSProperties => ({ animationDelay: `${ms}ms` });

/** A tiny menu bar with a status item, for "recording in the menu bar helper". */
const MenuBarIcon = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 16 16"
    fill="none"
    aria-hidden="true"
  >
    <rect
      x="1.5"
      y="2.5"
      width="13"
      height="11"
      rx="2.5"
      stroke="currentColor"
      strokeWidth="1.4"
    />
    <path d="M1.5 6h13" stroke="currentColor" strokeWidth="1.4" />
    <circle cx="11.75" cy="4.25" r="1" fill="currentColor" />
  </svg>
);

/** Animates its width to fit whatever it currently contains. */
function Morph({
  className,
  children,
}: {
  className: string;
  children: ReactNode;
}) {
  const inner = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number>();
  useLayoutEffect(() => {
    const el = inner.current;
    if (!el) return;
    const measure = () => setWidth(el.offsetWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <div className={className} style={{ width }}>
      <div ref={inner} className="w-max">
        {children}
      </div>
    </div>
  );
}

/** The record button. When recording starts it grows into the live control: status, timer, mark, stop. */
function RecordControl({ p }: { p: Props }) {
  const live = p.rec !== null || p.remote !== null;
  const disabled = p.starting || p.backendDown;
  return (
    <Morph
      className={`record-control ${live ? "is-live" : "is-idle"} ${!live && disabled ? "is-disabled" : ""}`}
    >
      {live ? (
        <div
          key="live"
          className="flex h-[50px] animate-fade-in items-center pl-4 pr-[5px]"
        >
          <Pulse />
          <span className="ml-2.5 hidden text-label font-medium text-ink sm:inline">
            Recording
          </span>
          <span
            className="ml-2 min-w-[5ch] font-mono text-body font-medium tabular-nums text-ink"
            aria-live="off"
          >
            {formatDuration(p.elapsed)}
          </span>
          <span className="mx-2 h-5 w-px bg-line-strong" aria-hidden="true" />
          <button
            onClick={p.onMark}
            disabled={p.stopping}
            className="btn btn-md btn-ghost px-3"
            data-tip="Flag this moment (M)"
          >
            <FlagIcon size={16} className="text-sun-deep" />
            Mark
            <kbd className="kbd hidden sm:inline-flex">M</kbd>
          </button>
          <button
            onClick={p.onStop}
            disabled={p.stopping}
            className="btn btn-md btn-record ml-1 px-4"
          >
            {p.stopping ? (
              <Spinner className="h-4 w-4" />
            ) : (
              <StopIcon size={16} />
            )}
            {p.stopping ? "Saving…" : "Stop"}
          </button>
        </div>
      ) : (
        <button
          key="idle"
          onClick={p.onStart}
          disabled={disabled}
          className="group flex h-[50px] animate-fade-in items-center gap-2.5 px-[26px] text-body font-semibold outline-none disabled:cursor-not-allowed"
        >
          {p.starting ? (
            <Spinner className="h-4 w-4" />
          ) : (
            <span className="h-2.5 w-2.5 rounded-full bg-on-accent shadow-[0_0_0_3px_color-mix(in_srgb,var(--on-accent)_30%,transparent)] transition-transform duration-200 ease-spring group-hover:scale-[1.2]" />
          )}
          {p.starting ? "Starting…" : "Start recording"}
        </button>
      )}
    </Morph>
  );
}

function ModelNote({
  tone,
  label,
  value,
}: {
  tone: "forest" | "trail" | "sun";
  label: string;
  value: string;
}) {
  const dot = { forest: "bg-forest", trail: "bg-trail", sun: "bg-sun" }[tone];
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
      <span className="shrink-0">{label}</span>
      <span className="truncate font-medium text-ink">{value}</span>
    </span>
  );
}

export default function Recorder(p: Props) {
  const recording = p.rec !== null || p.remote !== null;
  const helper = p.rec === null && p.remote !== null;
  const drafts = p.remote && !p.rec ? p.remote.drafts : p.drafts;
  const bookmarks = p.remote && !p.rec ? p.remote.bookmarks : p.bookmarks;
  const liveDraft = p.remote && !p.rec ? p.remote.draft : p.liveDraft;
  const draftBox = useRef<HTMLDivElement>(null);
  const remote = p.health?.transcription.engine === "remote";

  // Press M (while this window is focused) to flag the current moment.
  useEffect(() => {
    if (!recording) return;
    const onKey = (e: KeyboardEvent) => {
      const typing =
        e.target instanceof HTMLElement &&
        e.target.closest("input, textarea, select, [contenteditable]");
      if (
        !typing &&
        !e.metaKey &&
        !e.ctrlKey &&
        !e.altKey &&
        e.key.toLowerCase() === "m"
      )
        p.onMark();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [recording, p.onMark]);

  useEffect(() => {
    const box = draftBox.current;
    if (box) box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
  }, [drafts.length]);

  return (
    <div className="flex flex-col">
      {/* atmospheric header */}
      <div className="relative animate-enter">
        <div
          aria-hidden="true"
          className="absolute inset-x-[10%] bottom-0 top-1/3 rounded-hero bg-forest/10 blur-3xl"
        />
        <div className="art-frame h-[clamp(168px,20vw+48px,250px)]">
          <TrailScene
            parallax
            walking={recording}
            dusk={p.dusk}
            className="absolute inset-0 h-full w-full"
          />
        </div>
      </div>

      <div
        key={recording ? "live" : "idle"}
        className="mt-8 animate-enter"
        style={delay(60)}
      >
        <p className="eyebrow mb-2.5">
          {formatLongDate(new Date().toISOString())}
        </p>
        <h1 className="hero-title">
          {recording ? "On the trail" : "Ready when you are"}
        </h1>
        <p className="hero-sub mt-3">
          {helper
            ? "The menu bar helper is recording. Press M to flag a moment; the accurate transcript is made when you stop."
            : recording
              ? "Trailmix is listening. Press M to flag a moment; the accurate transcript is made when you stop."
              : `Trailmix quietly records, transcribes, and organizes your conversations${remote ? "." : " locally."}`}
        </p>
      </div>

      <div className="mt-7 animate-enter" style={delay(120)}>
        <RecordControl p={p} />
        {recording && bookmarks.length > 0 && (
          <div className="mt-4 flex flex-wrap gap-1.5">
            {bookmarks.map((b, i) => (
              <span
                key={i}
                className="chip animate-pop bg-sun-soft text-sun-deep"
              >
                <FlagIcon size={12} /> {formatDuration(b.t)}
              </span>
            ))}
          </div>
        )}
      </div>

      {p.backendDown && !recording && (
        <div className="mt-8 flex animate-enter items-start gap-3 rounded-lg border border-trail/20 bg-trail-soft/70 p-4 text-label text-trail-deep">
          <AlertIcon size={18} className="mt-0.5 shrink-0" />
          <div>
            <div className="font-semibold">
              Can't reach the Trailmix backend
            </div>
            <div className="mt-0.5 leading-relaxed">
              Open Trailmix.app, or run{" "}
              <code className="rounded-xs bg-surface/70 px-1.5 py-0.5 font-mono text-meta">
                ./trailmix
              </code>{" "}
              in the Trailmix folder. This page reconnects on its own.
            </div>
          </div>
        </div>
      )}

      <div className="mt-14 animate-enter sm:mt-16" style={delay(180)}>
        {recording ? (
          <section
            aria-label="Live recording"
            className="glass overflow-hidden"
          >
            <div className="px-5 py-5 sm:px-6">
              {p.rec ? (
                <LevelMeters rec={p.rec} />
              ) : (
                <div className="flex items-center gap-3 text-hint text-ink-soft">
                  <span className="well well-forest">
                    <MenuBarIcon />
                  </span>
                  <div>
                    <div className="text-label font-medium text-ink">
                      Recording in the menu bar helper
                    </div>
                    <div>
                      {p.remote?.has_system
                        ? "Your mic and the meeting's audio"
                        : "Your mic"}{" "}
                      · levels are shown in its menu
                    </div>
                  </div>
                </div>
              )}
            </div>

            {p.notice && p.rec && (
              <div className="flex items-start gap-2.5 border-t border-line bg-sun-soft/60 px-5 py-3 text-ui leading-relaxed text-sun-deep sm:px-6">
                <AlertIcon size={16} className="mt-0.5 shrink-0" />
                {p.notice}
              </div>
            )}

            {liveDraft ? (
              <div className="border-t border-line px-5 py-5 sm:px-6">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <LinesIcon size={16} className="text-forest" />
                    <span className="text-label font-medium">Live draft</span>
                    <span className="chip bg-surface-subtle text-ink-soft">
                      rough
                    </span>
                  </div>
                  <span className="text-meta text-ink-soft">
                    The accurate transcript is made after you stop
                  </span>
                </div>
                <div
                  ref={draftBox}
                  className="fade-edges-y -mx-1 max-h-80 space-y-2.5 overflow-y-auto px-1 py-1"
                >
                  {drafts.length === 0 ? (
                    <p className="flex items-center gap-2 text-label text-ink-soft">
                      <Spinner /> Listening…
                    </p>
                  ) : (
                    drafts.map((d, i) => (
                      <p
                        key={i}
                        className="flex animate-enter items-start gap-2.5 text-body leading-relaxed"
                      >
                        {d.speaker && (
                          <span className="pt-[3px]">
                            <SpeakerChip speaker={d.speaker} />
                          </span>
                        )}
                        <span>{d.text}</span>
                      </p>
                    ))
                  )}
                </div>
              </div>
            ) : (
              <p className="border-t border-line px-5 py-4 text-ui text-ink-soft sm:px-6">
                Recording to disk. The transcript and summary are made after you
                stop.
              </p>
            )}
          </section>
        ) : (
          <section
            aria-label="Recording setup"
            className="glass overflow-hidden"
          >
            {p.native ? (
              <div className="divide-y divide-line px-5 sm:px-6">
                <ControlRow
                  icon={<MicIcon size={16} />}
                  label="Microphone"
                  hint={
                    p.native.mic_allowed === false
                      ? "Not allowed yet: turn it on in System Settings → Privacy & Security → Microphone."
                      : undefined
                  }
                >
                  <span className="min-w-0 truncate text-right text-label text-ink-soft">
                    {p.native.mic}
                  </span>
                </ControlRow>
                <ControlRow
                  icon={<MonitorIcon size={16} />}
                  label="Meeting audio"
                  hint="Straight from your call apps: Zoom, Meet in your browser, Teams, FaceTime. No sharing picker."
                >
                  <span className="min-w-0 truncate text-right text-label text-ink-soft">
                    {p.native.source}
                  </span>
                </ControlRow>
                <p className="flex items-center gap-2 py-3 text-hint text-ink-soft">
                  <MenuBarIcon />
                  Records with the Trailmix menu bar recorder
                  {p.native.machine ? ` on ${p.native.machine}` : ""}. Change
                  these in its menu.
                </p>
                <details className="group py-3">
                  <summary className="cursor-pointer list-none text-label font-medium text-ink [&::-webkit-details-marker]:hidden">
                    Test your audio{" "}
                    <span className="font-normal text-ink-soft group-open:hidden">· plays a chime and checks both sides are heard</span>
                  </summary>
                  <div className="mt-2">
                    <SoundCheck native={p.native} />
                  </div>
                </details>
              </div>
            ) : (
              <div className="divide-y divide-line px-5 sm:px-6">
                <MicPicker value={p.micDeviceId} onChange={p.onMicChange} />
                <SwitchRow
                  icon={<MonitorIcon size={16} />}
                  label="Capture meeting audio"
                  hint="Pick the tab your call is in and tick “Share tab audio”. Enables You / Them labels."
                  checked={p.includeMeetingAudio}
                  onChange={p.onIncludeMeetingAudio}
                />
                <SwitchRow
                  icon={<LinesIcon size={16} />}
                  label="Live draft"
                  hint="A rough running transcript from a small model. Turn off for the lightest footprint."
                  checked={p.liveDraft}
                  onChange={p.onLiveDraft}
                />
              </div>
            )}
            {p.health && (
              <footer className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-line bg-surface-subtle/50 px-5 py-3 text-meta text-ink-soft sm:px-6">
                <ModelNote
                  tone="forest"
                  label={remote ? "Transcript · remote" : "Transcript"}
                  value={shortModel(p.health.transcription.model)}
                />
                <ModelNote
                  tone={p.health.summary.ready ? "forest" : "trail"}
                  label={`Summary · ${p.health.summary.label}`}
                  value={
                    p.health.summary.ready
                      ? shortModel(p.health.summary.model)
                      : "not set up"
                  }
                />
                {p.health.fallback && (
                  <ModelNote
                    tone={p.health.fallback.ready ? "sun" : "trail"}
                    label="Fallback"
                    value={p.health.fallback.label}
                  />
                )}
              </footer>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
