import { useEffect, useState } from "react";
import { api } from "../api";
import type { SystemInfo } from "../api";
import { ChevronDownIcon } from "./icons";
import { Collapse } from "./ui";

const OPEN_KEY = "trailmix.conditionsOpen";

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="rounded-sm bg-forest/[.04] px-2.5 py-1.5">
      <div className="text-caption text-ink-soft">{label}</div>
      <div className={`text-hint font-semibold tabular-nums ${warn ? "text-trail-deep" : "text-ink"}`}>{value}</div>
    </div>
  );
}

/** Live resource readout, framed as the weather for the trail ahead. */
export default function SystemStatus() {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem(OPEN_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggle = () => {
    setOpen(!open);
    try {
      localStorage.setItem(OPEN_KEY, open ? "0" : "1");
    } catch {
      /* ignore */
    }
  };

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .system()
        .then((s) => alive && setSys(s))
        .catch(() => alive && setSys(null));
    void load();
    const t = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (!sys) return null;
  const transcribeNeeds = sys.final_model_gb + sys.headroom_gb;
  const ramLow = sys.ram_available_gb < transcribeNeeds;
  const diskLow = sys.disk_free_gb < 5;
  const swapHigh = sys.swap_used_gb > 8;
  // When models would wait: short on memory and swapping is a bad idea (resources.check).
  const swapRoomLow = sys.disk_free_percent < sys.swap_disk_free_percent;
  const critical = sys.memory_pressure >= 4;
  const dire = ramLow && (swapRoomLow || critical);

  const weather = diskLow || dire
    ? { label: "Stormy", cls: "bg-trail-soft text-trail-deep", dot: "bg-trail" }
    : ramLow || swapHigh
      ? { label: "Cloudy", cls: "bg-sun-soft text-sun-deep", dot: "bg-sun" }
      : { label: "Clear skies", cls: "bg-forest-soft text-forest-deep", dot: "bg-forest" };
  const freePct = Math.min(100, (sys.ram_available_gb / sys.ram_total_gb) * 100);

  return (
    <div className="px-3 pb-4 text-meta">
      <button
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full items-center justify-between rounded-md px-2.5 py-2 transition-colors hover:bg-forest/[.05]"
      >
        <span className="flex items-center gap-1.5 font-medium text-ink-soft">
          Trail conditions
          <ChevronDownIcon size={16} className={`text-ink-faint transition-transform duration-200 ${open ? "" : "-rotate-90"}`} />
        </span>
        <span className={`chip ${weather.cls}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${weather.dot}`} />
          {weather.label}
        </span>
      </button>

      <div className="px-2.5 pt-1">
        <div className="mb-1.5 flex justify-between text-ink-soft">
          <span>Memory free</span>
          <span className={`font-semibold tabular-nums ${ramLow ? "text-sun-deep" : "text-ink"}`}>
            {sys.ram_available_gb} of {sys.ram_total_gb} GB
          </span>
        </div>
        <div className="h-1 overflow-hidden rounded-full bg-[var(--switch-off)]">
          <div
            className={`h-full rounded-full transition-[width] duration-700 ease-out ${ramLow ? "bg-sun" : "bg-forest"}`}
            style={{ width: `${freePct}%` }}
          />
        </div>
      </div>

      <Collapse open={open}>
        <div className="px-2.5 pt-3">
          <div className="grid grid-cols-2 gap-1.5">
            <Stat label="Swap" value={`${sys.swap_used_gb} GB`} warn={swapHigh} />
            <Stat label="CPU" value={`${Math.round(sys.cpu_percent)}%`} />
            <Stat label="Disk free" value={`${sys.disk_free_gb} GB`} warn={diskLow} />
            <Stat label="Audio" value={sys.audio_gb < 1 ? `${Math.round(sys.audio_gb * 1024)} MB` : `${sys.audio_gb} GB`} />
          </div>
          {ramLow && !dire && (
            <p className="mt-2.5 leading-snug text-sun-deep">
              Under ~{transcribeNeeds.toFixed(1)} GB free: models still run, with macOS using swap, so they may be slower.
            </p>
          )}
          {dire && (
            <p className="mt-2.5 leading-snug text-trail-deep">
              {critical
                ? "macOS reports critical memory pressure, so models wait (or you can proceed anyway)."
                : `Under ${sys.swap_disk_free_percent}% of the disk is free for swap, so models wait until there's memory.`}
            </p>
          )}
          {diskLow && <p className="mt-2.5 leading-snug text-trail-deep">Disk is nearly full. Long recordings and model downloads may fail.</p>}
        </div>
      </Collapse>
    </div>
  );
}
