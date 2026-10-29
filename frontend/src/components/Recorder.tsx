import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import type {
  Bookmark,
  DraftLine,
  Health,
  LiveRecording,
  NativeRecorder,
} from "../api";
import { hasCapability, missingCapabilityReason } from "../api";
import type { ActiveRecording } from "../capture";
import { formatDuration, formatLongDate, shortModel } from "../format";
import { cap, platformWords } from "../platform";
import {
  AlertIcon,
  ChevronDownIcon,
  CompassIcon,
  FlagIcon,
  LinesIcon,
  MicIcon,
  MonitorIcon,
  SparkleIcon,
  StopIcon,
} from "./icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { TrailScene } from "./illustrations";
import LevelMeters from "./LevelMeter";
import MicPicker from "./MicPicker";
import { CatchUpAnswers, CatchUpAsk, useCatchUp } from "./CatchUp";
import { LiveAttachments } from "./Attachments";
import NotesEditor from "./NotesEditor";
import SoundCheck from "./SoundCheck";
import { Collapse, ControlRow, Pulse, Segmented, SpeakerChip, Spinner, SwitchRow } from "./ui";

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
  /** Switches the speech engine for recordings from now on (the one in Settings → Transcription). */
  onEngine: (engine: "local" | "gemini") => void;
  notice: string | null;
  drafts: DraftLine[];
  /** The words being said right now (live captions from Google) */
  interims: DraftLine[];
  /** The recording as the engine sees it, whichever app captures it (for the notes written during it). */
  live: LiveRecording | null;
  health: Health | null;
  backendDown: boolean;
  bookmarks: Bookmark[];
  dusk: boolean;
  onStart: () => void;
  onStop: () => void;
  onMark: () => void;
}

const delay = (ms: number): CSSProperties => ({ animationDelay: `${ms}ms` });

/** A tiny menu bar with a status item, for "recording in the menu bar helper" (a taskbar at the bottom, off a Mac). */
const MenuBarIcon = () => (
  <svg
    className={platformWords().os === "macos" ? undefined : "-scale-y-100"}
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

/** The record button. While recording, the notepad's bar takes over (timer, Mark, Stop). */
function RecordControl({ p }: { p: Props }) {
  const disabled = p.starting || p.backendDown;
  return (
    <Morph className={`record-control is-idle ${disabled ? "is-disabled" : ""}`}>
      <button
        key="idle"
        onClick={p.onStart}
        data-testid="record"
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

/** Where recordings are transcribed, switchable right before you press Record. */
function EnginePicker({ health, onEngine }: { health: Health | null; onEngine: (e: "local" | "gemini") => void }) {
  const engine = health?.transcription.engine;
  if (!health || !engine) return null;
  return (
    <ControlRow
      icon={<LinesIcon size={16} />}
      label="Transcribe with"
      hint={
        engine === "gemini"
          ? "Google: live captions as you talk, then a transcript with the speakers told apart. Your audio is sent to Google."
          : engine === "remote"
            ? "Your own transcription server (change it in Settings → Transcription)."
            : `${cap(platformWords().thisComputer)}: private and free, labels you and the other side of a call.`
      }
    >
      <Segmented
        label="Speech engine for the next recording"
        size="sm"
        value={engine === "gemini" ? "gemini" : "local"}
        onChange={(v) => onEngine(v as "local" | "gemini")}
        options={[
          { id: "local", label: cap(platformWords().thisComputer) },
          { id: "gemini", label: "Google" },
        ]}
      />
    </ControlRow>
  );
}

/** What the recording looks like, whichever app captures it: this window or the menu bar helper. */
function liveView(p: Props) {
  const fromHelper = p.remote !== null && !p.rec;
  return {
    helper: fromHelper,
    meetingId: p.rec?.meetingId ?? p.remote?.id ?? 0,
    drafts: fromHelper ? p.remote!.drafts : p.drafts,
    interims: fromHelper ? (p.remote!.interims ?? []) : p.interims,
    bookmarks: fromHelper ? p.remote!.bookmarks : p.bookmarks,
    liveDraft: fromHelper ? p.remote!.draft : p.liveDraft,
    // With Gemini the captions stream from Google as you talk; the transcript (with speakers told apart) follows after.
    gemini: p.health?.transcription.engine === "gemini",
    finalLive: !!p.health?.transcription.live_final,
  };
}

export default function Recorder(p: Props) {
  const recording = p.rec !== null || p.remote !== null;
  const gemini = p.health?.transcription.engine === "gemini";
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

  if (recording) return <Notepad p={p} />;

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
            dusk={p.dusk}
            className="absolute inset-0 h-full w-full"
          />
        </div>
      </div>

      <div className="mt-8 animate-enter" style={delay(60)}>
        <p className="eyebrow mb-2.5">
          {formatLongDate(new Date().toISOString())}
        </p>
        <h1 className="hero-title">Ready when you are</h1>
        <p className="hero-sub mt-3">
          Trailmix quietly records, transcribes, and organizes your conversations{remote ? "." : " locally."} While
          it records, you get a blank page for your own notes.
        </p>
      </div>

      <div className="mt-7 animate-enter" style={delay(120)}>
        <RecordControl p={p} />
      </div>

      {p.backendDown && (
        <div className="mt-8 flex animate-enter items-start gap-3 rounded-lg border border-trail/20 bg-trail-soft/70 p-4 text-label text-trail-deep">
          <AlertIcon size={18} className="mt-0.5 shrink-0" />
          <div>
            <div className="font-semibold">
              Can't reach the Trailmix backend
            </div>
            <div className="mt-0.5 leading-relaxed">
              Open {platformWords().appName}, or run{" "}
              <code className="rounded-xs bg-surface/70 px-1.5 py-0.5 font-mono text-meta">
                ./trailmix
              </code>{" "}
              in the Trailmix folder. This page reconnects on its own.
            </div>
          </div>
        </div>
      )}

      <div className="mt-14 animate-enter sm:mt-16" style={delay(180)}>
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
                  ? platformWords().os === "windows"
                    ? "Not allowed yet: turn it on in Settings → Privacy & security → Microphone."
                    : "Not allowed yet: turn it on in System Settings → Privacy & Security → Microphone."
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
              hint={
                !hasCapability(p.native, "system_audio")
                  ? `${missingCapabilityReason(p.native, "system_audio")} Only your mic is recorded.`
                  : !hasCapability(p.native, "per_app_audio")
                    ? `Every app's audio together. ${missingCapabilityReason(p.native, "per_app_audio")}`
                    : "Straight from your call apps: Zoom, Meet in your browser, Teams, FaceTime. No sharing picker."
              }
            >
              <span className="min-w-0 truncate text-right text-label text-ink-soft">
                {hasCapability(p.native, "system_audio") ? p.native.source : "Your mic only"}
              </span>
            </ControlRow>
            <EnginePicker health={p.health} onEngine={p.onEngine} />
            <p className="flex items-center gap-2 py-3 text-hint text-ink-soft">
              <MenuBarIcon />
              Records with the Trailmix {platformWords().recorderName}
              {p.native.machine ? ` on ${p.native.machine}` : ""}. Change
              these in its menu.
            </p>
            {hasCapability(p.native, "sound_check") ? (
              <details className="group py-3">
                <summary className="cursor-pointer list-none text-label font-medium text-ink [&::-webkit-details-marker]:hidden">
                  Test your audio{" "}
                  <span className="font-normal text-ink-soft group-open:hidden">
                    · plays a chime and checks both sides are heard
                  </span>
                </summary>
                <div className="mt-2">
                  <SoundCheck native={p.native} />
                </div>
              </details>
            ) : (
              <p className="py-3 text-hint text-ink-soft">
                {missingCapabilityReason(p.native, "sound_check")}
              </p>
            )}
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
              label={gemini ? "Live captions" : "Live draft"}
              hint={
                gemini
                  ? "Words appear a second after they're said, streamed from Google. Off: the transcript comes after you stop."
                  : "A rough running transcript from a small model. Turn off for the lightest footprint."
              }
              checked={p.liveDraft}
              onChange={p.onLiveDraft}
            />
            <EnginePicker health={p.health} onEngine={p.onEngine} />
          </div>
        )}
        {p.health && (
          <footer className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-line bg-surface-subtle/50 px-5 py-3 text-meta text-ink-soft sm:px-6">
            <ModelNote
              tone="forest"
              label={gemini ? "Transcript · Google" : remote ? "Transcript · remote" : "Transcript"}
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
      </div>
    </div>
  );
}

type BarTab = "ask" | "transcript" | "notes";

/**
 * While recording: a blank page for your notes, and everything else in a bar at the bottom. The bar shows the
 * timer, Mark and Stop, the latest words said, and Catch me up; it opens upward onto the answers, the live
 * transcript and the notes written so far.
 */
function Notepad({ p }: { p: Props }) {
  const v = liveView(p);
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<BarTab>("ask");
  const c = useCatchUp(v.meetingId, () => {
    setTab("ask");
    setOpen(true);
  });
  const elapsed = useRef(p.elapsed);
  elapsed.current = p.elapsed;
  const panel = useRef<HTMLDivElement>(null);

  // A timestamp clicked in a catch-up answer: that line of the live transcript, highlighted for a moment. New
  // lines don't scroll it away for a few seconds.
  const [seeked, setSeeked] = useState<number | null>(null);
  const [seekTo, setSeekTo] = useState<number | null>(null);
  const holdUntil = useRef(0);
  const seek = (t: number) => {
    setTab("transcript");
    setOpen(true);
    setSeekTo(t);
  };
  useEffect(() => {
    const box = panel.current;
    if (seekTo === null || tab !== "transcript" || !box) return;
    const lines = [...box.querySelectorAll<HTMLElement>("[data-start]")];
    const line = lines.filter((el) => Number(el.dataset.start) <= seekTo + 1).pop() ?? lines[0];
    setSeekTo(null);
    if (!line) return;
    holdUntil.current = Date.now() + 8000;
    box.scrollTo({
      top: box.scrollTop + line.getBoundingClientRect().top - box.getBoundingClientRect().top - box.clientHeight / 3,
      behavior: "smooth",
    });
    setSeeked(Number(line.dataset.start));
    setTimeout(() => setSeeked(null), 2500);
  }, [seekTo, tab]);

  // The transcript follows what's being said, unless you just jumped to a moment in it.
  useEffect(() => {
    const box = panel.current;
    if (box && open && tab === "transcript" && Date.now() > holdUntil.current)
      box.scrollTo({ top: box.scrollHeight, behavior: "smooth" });
  }, [v.drafts.length, open, tab]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const latest = v.interims.length ? v.interims[v.interims.length - 1] : v.drafts[v.drafts.length - 1];
  const toggle = (t: BarTab) => {
    if (open && tab === t) setOpen(false);
    else {
      setTab(t);
      setOpen(true);
    }
  };

  return (
    <div className="flex min-h-[calc(100dvh-10rem)] flex-col">
      <header className="animate-enter">
        <p className="eyebrow mb-2">
          {formatLongDate(new Date().toISOString())}
          {v.helper ? ` · recording in the ${platformWords().helperName}` : ""}
        </p>
        <h1 className="font-display text-[2rem] font-medium leading-tight tracking-[-0.02em] text-ink">
          {p.live?.title || "New meeting"}
        </h1>
        {v.bookmarks.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {v.bookmarks.map((b, i) => (
              <span key={i} className="chip animate-pop bg-sun-soft text-sun-deep">
                <FlagIcon size={12} /> {formatDuration(b.t)}
              </span>
            ))}
          </div>
        )}
      </header>

      <div className="mt-6 flex-1 animate-enter" style={delay(60)}>
        {/* Loaded once the recording's notes have arrived, so a page reopened mid-meeting starts from them. */}
        {p.live && p.live.id === v.meetingId ? (
          <NotesEditor
            key={p.live.id}
            meetingId={p.live.id}
            notes={p.live.my_notes}
            now={() => elapsed.current}
            autoFocus
            placeholder="Write your notes here. # makes a heading, - a list. The AI builds the meeting's notes around them."
          />
        ) : (
          <p className="flex items-center gap-2 text-label text-ink-soft">
            <Spinner /> Getting your notes ready…
          </p>
        )}
      </div>

      {v.meetingId > 0 && (
        <div className="mt-8 border-t border-line pt-5 animate-enter" style={delay(90)}>
          <LiveAttachments meetingId={v.meetingId} />
        </div>
      )}

      <div className="sticky bottom-4 z-20 mt-10 animate-enter" style={delay(120)}>
        <section aria-label="Live recording" className="glass overflow-hidden !bg-surface shadow-lg">
          <Collapse open={open}>
            <div className="border-b border-line">
              <div className="flex items-center justify-between gap-2 px-4 pt-3 sm:px-5">
                <Segmented
                  kind="tab"
                  size="sm"
                  label="Live recording"
                  value={tab}
                  onChange={setTab}
                  options={[
                    { id: "ask", label: "Catch me up" },
                    { id: "transcript", label: v.gemini ? "Live captions" : "Transcript" },
                    { id: "notes", label: "Notes so far" },
                  ]}
                />
                <button className="icon-btn h-8 w-8" onClick={() => setOpen(false)} aria-label="Close" data-tip="Close (Esc)">
                  <ChevronDownIcon size={16} />
                </button>
              </div>
              <div ref={panel} className="max-h-[min(50vh,480px)] overflow-y-auto px-4 py-4 sm:px-5">
                {tab === "ask" && <CatchUpAnswers c={c} onSeek={seek} />}
                {tab === "transcript" && <LiveTranscript p={p} v={v} seeked={seeked} />}
                {tab === "notes" && <LiveNotes notes={p.live?.notes ?? []} status={p.live?.notes_status ?? null} />}
              </div>
            </div>
          </Collapse>

          {p.notice && p.rec && (
            <div className="flex items-start gap-2.5 border-b border-line bg-sun-soft/60 px-4 py-2.5 text-ui leading-relaxed text-sun-deep sm:px-5">
              <AlertIcon size={16} className="mt-0.5 shrink-0" />
              {p.notice}
            </div>
          )}

          <div className="flex items-center gap-1.5 px-3 py-2 sm:gap-2 sm:px-4">
            <Pulse />
            <span className="ml-1 min-w-[5ch] font-mono text-label font-medium tabular-nums text-ink" aria-live="off">
              {formatDuration(p.elapsed)}
            </span>
            <button
              onClick={() => toggle("transcript")}
              className="mx-1 flex min-w-0 flex-1 items-center gap-2 rounded-sm px-1.5 py-1 text-left text-label text-ink-soft transition-colors hover:bg-surface-subtle"
              data-tip={v.liveDraft ? "Show the transcript" : undefined}
              data-private="live transcript"
            >
              {latest ? (
                <>
                  {latest.speaker && <SpeakerChip speaker={latest.speaker} />}
                  <span className="truncate">{latest.text}</span>
                </>
              ) : (
                <span className="truncate">{v.liveDraft ? "Listening…" : "Recording to disk"}</span>
              )}
            </button>
            <button
              onClick={p.onMark}
              disabled={p.stopping}
              className="btn btn-sm btn-ghost shrink-0 px-2.5"
              data-tip="Flag this moment (M when not typing)"
            >
              <FlagIcon size={15} className="text-sun-deep" />
              <span className="hidden sm:inline">Mark</span>
            </button>
            <button onClick={p.onStop} data-testid="stop" disabled={p.stopping} className="btn btn-sm btn-record shrink-0 px-3">
              {p.stopping ? <Spinner className="h-4 w-4" /> : <StopIcon size={15} />}
              {p.stopping ? "Saving…" : "Stop"}
            </button>
            <button
              className="icon-btn h-8 w-8 shrink-0"
              onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
              aria-label={open ? "Hide the transcript and answers" : "Show the transcript and answers"}
            >
              <ChevronDownIcon size={16} className={`transition-transform duration-200 ${open ? "" : "rotate-180"}`} />
            </button>
          </div>

          <div className="border-t border-line px-3 py-2.5 sm:px-4">
            {c.plan && !c.ready ? (
              <p className="flex items-center gap-2 text-meta text-ink-soft">
                <CompassIcon size={14} className="shrink-0 text-forest" />
                {c.plan.reason || "Catch me up isn't available right now."}
              </p>
            ) : (
              <CatchUpAsk c={c} />
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

/** The live transcript as it's made, with the levels (or where the helper shows them) above it. */
function LiveTranscript({ p, v, seeked }: { p: Props; v: ReturnType<typeof liveView>; seeked: number | null }) {
  return (
    <div>
      <div className="mb-4">
        {p.rec ? (
          <LevelMeters rec={p.rec} />
        ) : (
          <div className="flex items-center gap-3 text-hint text-ink-soft">
            <span className="well well-forest">
              <MenuBarIcon />
            </span>
            <div>
              <div className="text-label font-medium text-ink">Recording in the {platformWords().helperName}</div>
              <div>{p.remote?.has_system ? "Your mic and the meeting's audio" : "Your mic"} · levels are shown in its menu</div>
            </div>
          </div>
        )}
      </div>
      {v.liveDraft ? (
        <>
          <p className="mb-3 flex flex-wrap items-center gap-2 text-meta text-ink-soft">
            {!v.finalLive && <span className="chip bg-surface-subtle text-ink-soft">rough</span>}
            {v.gemini
              ? "From Google as you talk; the transcript with speakers follows after you stop"
              : v.finalLive
                ? "Made by the accurate model as you talk"
                : "The accurate transcript is made after you stop"}
          </p>
          <div data-private="live transcript" className="space-y-2.5">
            {v.drafts.length === 0 && v.interims.length === 0 ? (
              <p className="flex items-center gap-2 text-label text-ink-soft">
                <Spinner /> Listening…
              </p>
            ) : (
              [...v.drafts, ...v.interims.map((d) => ({ ...d, interim: true }))].map((d, i) => (
                <p
                  key={i}
                  data-start={"interim" in d ? undefined : d.start}
                  className={`-mx-1.5 flex animate-enter items-start gap-2.5 rounded-sm px-1.5 text-body leading-relaxed transition-colors duration-500 ${
                    "interim" in d ? "text-ink-soft" : ""
                  } ${seeked !== null && d.start === seeked && !("interim" in d) ? "bg-sun-soft" : ""}`}
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
        </>
      ) : (
        <p className="text-ui text-ink-soft">Recording to disk. The transcript and summary are made after you stop.</p>
      )}
    </div>
  );
}

const PLUGINS = [remarkGfm];

/** "Notes so far": what the notes AI wrote during the meeting (live_notes.py), newest stretch last. */
function LiveNotes({ notes, status }: { notes: LiveRecording["notes"]; status: string | null }) {
  const paused = !!status && /^(Paused|Couldn't)/.test(status);
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <SparkleIcon size={16} className="text-forest" />
          <span className="text-label font-medium">Notes so far</span>
        </div>
        {status && (
          <span className={`flex items-center gap-1.5 text-meta ${paused ? "text-sun-deep" : "text-ink-soft"}`}>
            {status.startsWith("Writing") && <Spinner className="h-3 w-3" />}
            {status}
          </span>
        )}
      </div>
      {notes.length === 0 ? (
        <p className="text-label text-ink-soft">The first notes come after ten minutes of meeting.</p>
      ) : (
        <div data-private="notes" className="prose-summary prose-draft space-y-5 text-label">
          {notes.map((n) => (
            <div key={n.start}>
              <h3>
                {formatDuration(n.start)} to {formatDuration(n.end)}
              </h3>
              <ReactMarkdown remarkPlugins={PLUGINS}>{n.text}</ReactMarkdown>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
