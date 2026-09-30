import { useEffect, useRef, useState } from "react";
import type { ActiveRecording } from "../capture";
import { MicIcon, MonitorIcon } from "./icons";

const SILENT_AFTER_MS = 8000;
const HEARD = 0.12;
const STEP_MS = 60; // one bar per step, scrolling right to left
const BAR = 3;
const GAP = 3;

/** A scrolling waveform of one track's input level, drawn on a canvas. */
function Wave({ read, color }: { read: () => number | null; color: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const c = canvas.current;
    const ctx = c?.getContext("2d");
    if (!c || !ctx) return;
    const history: number[] = [];
    let raf = 0;
    let last = 0;
    let smooth = 0;
    let fill = "";
    let frame = 0;

    const draw = (t: number) => {
      raf = requestAnimationFrame(draw);
      if (t - last < STEP_MS) return;
      last = t;
      if (frame++ % 40 === 0) fill = getComputedStyle(document.documentElement).getPropertyValue(color).trim() || "#4f86a8";
      const dpr = window.devicePixelRatio || 1;
      const w = c.clientWidth;
      const h = c.clientHeight;
      if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
        c.width = Math.round(w * dpr);
        c.height = Math.round(h * dpr);
      }
      const level = read() ?? 0;
      smooth = Math.max(level, smooth * 0.72);
      history.push(smooth);
      const n = Math.ceil(w / (BAR + GAP));
      while (history.length > n) history.shift();

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = fill;
      history.forEach((v, i) => {
        const age = history.length - i;
        const x = w - age * (BAR + GAP);
        const bh = Math.max(2, Math.min(1, v * 1.15) * (h - 2));
        ctx.globalAlpha = 0.25 + 0.75 * Math.max(0, 1 - age / n) ** 0.6;
        ctx.beginPath();
        ctx.roundRect(x, (h - bh) / 2, BAR, bh, 1.5);
        ctx.fill();
      });
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [read, color]);

  return <canvas ref={canvas} className="h-10 w-full" aria-hidden="true" />;
}

function Track({ tone, icon, label, sub, read }: { tone: "sky" | "berry"; icon: JSX.Element; label: string; sub: string; read: () => number | null }) {
  return (
    <div className="min-w-0 flex-1">
      <div className="mb-2 flex items-center gap-2 text-hint">
        <span className={tone === "sky" ? "text-sky-deep" : "text-berry-deep"}>{icon}</span>
        <span className="font-medium text-ink">{label}</span>
        <span className="truncate text-ink-soft">{sub}</span>
      </div>
      <Wave read={read} color={tone === "sky" ? "--sky" : "--berry"} />
    </div>
  );
}

/** Live input levels for each track, so you can see Trailmix is actually hearing both sides. */
export default function LevelMeters({ rec }: { rec: ActiveRecording }) {
  const [hasSystem] = useState(() => rec.levels().system !== null);
  const [micSilent, setMicSilent] = useState(false);
  const lastHeard = useRef(Date.now());
  const readers = useRef({
    mic: () => {
      const v = rec.levels().mic;
      if (v > HEARD) lastHeard.current = Date.now();
      return v;
    },
    system: () => rec.levels().system,
  });

  useEffect(() => {
    const t = setInterval(() => setMicSilent(Date.now() - lastHeard.current > SILENT_AFTER_MS), 1000);
    return () => clearInterval(t);
  }, []);

  return (
    <div>
      <div className="flex flex-col gap-5 sm:flex-row sm:gap-8">
        <Track tone="sky" icon={<MicIcon size={16} />} label="You" sub={rec.micLabel} read={readers.current.mic} />
        {hasSystem && <Track tone="berry" icon={<MonitorIcon size={16} />} label="Them" sub="shared tab audio" read={readers.current.system} />}
      </div>
      {micSilent && (
        <p className="mt-4 animate-fade-in rounded-md bg-sun-soft px-3 py-2 text-hint leading-snug text-sun-deep">
          We haven't heard your mic for a bit. If you're talking, check which microphone is in use ({rec.micLabel}); you can pick
          another one for your next recording.
        </p>
      )}
    </div>
  );
}
