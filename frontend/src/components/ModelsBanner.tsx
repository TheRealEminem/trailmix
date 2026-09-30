import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { SpeechModels } from "../api";
import { AlertIcon, CheckIcon, DownloadIcon } from "./icons";

/**
 * First run: the speech models (about 1.7 GB in all) download in the background. Shows how far along they are,
 * says what still works meanwhile, and offers a retry if the download failed. Hidden once everything is installed.
 */
export default function ModelsBanner() {
  const [info, setInfo] = useState<SpeechModels | null>(null);
  const [justFinished, setJustFinished] = useState(false);
  const wasDownloading = useRef(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .speechModels()
        .then((m) => alive && setInfo(m))
        .catch(() => undefined); // an older or unreachable server: just don't show anything
    void load();
    // Quick while something is downloading, lazy otherwise (a retry or a settings change can start one).
    const t = setInterval(load, info?.models.some((m) => m.downloading) ? 1500 : 10000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [info?.models.some((m) => m.downloading)]); // eslint-disable-line react-hooks/exhaustive-deps

  const missing = info?.local ? info.models.filter((m) => !m.installed) : [];
  const downloading = missing.some((m) => m.downloading);

  useEffect(() => {
    if (downloading) {
      wasDownloading.current = true;
    } else if (wasDownloading.current && info && missing.length === 0) {
      wasDownloading.current = false;
      setJustFinished(true);
      const t = setTimeout(() => setJustFinished(false), 4000);
      return () => clearTimeout(t);
    }
  }, [downloading, info, missing.length]);

  if (!info) return null;

  if (justFinished && missing.length === 0) {
    return (
      <div role="status" className="mb-6 flex animate-enter items-center gap-3 rounded-lg border border-forest/20 bg-forest/[.06] px-4 py-3 text-label text-forest">
        <CheckIcon size={18} className="shrink-0" />
        <span className="font-semibold">Speech models are ready.</span>
      </div>
    );
  }
  if (missing.length === 0) return null;

  const failed = missing.find((m) => m.error && !m.downloading);
  if (failed && !downloading) {
    return (
      <div role="alert" className="mb-6 flex animate-enter items-start gap-3 rounded-lg border border-trail/20 bg-trail-soft/70 p-4 text-label text-trail-deep">
        <AlertIcon size={18} className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="font-semibold">The speech model didn't download</div>
          <div className="mt-0.5 leading-relaxed">{failed.error}</div>
        </div>
        <button className="btn btn-sm shrink-0" onClick={() => void api.downloadSpeechModels().then(setInfo)}>
          Try again
        </button>
      </div>
    );
  }

  const current = missing.find((m) => m.downloading) ?? missing[0];
  const total = missing.reduce((sum, m) => sum + m.size_gb, 0);
  const pct = current.progress;
  return (
    <div role="status" className="mb-6 animate-enter rounded-lg border border-sky/25 bg-sky-soft/60 p-4 text-label text-sky-deep">
      <div className="flex items-start gap-3">
        <DownloadIcon size={18} className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="font-semibold">
            Getting Trailmix ready: downloading speech models ({total.toFixed(1)} GB, once)
          </div>
          <div className="mt-0.5 leading-relaxed">
            You can record right away. Live drafts and transcripts start as soon as the download finishes.
          </div>
        </div>
        {pct !== null && <span className="shrink-0 font-mono tabular-nums">{Math.round(pct * 100)}%</span>}
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-sky/15" aria-hidden="true">
        <div
          className={`h-full rounded-full bg-sky transition-[width] duration-700 ease-out ${pct === null ? "w-1/3 animate-pulse" : ""}`}
          style={pct === null ? undefined : { width: `${Math.max(2, pct * 100)}%` }}
        />
      </div>
      {missing.length > 1 && (
        <div className="mt-2 text-caption opacity-80">
          {current.label}. {missing.length - 1} more after this.
        </div>
      )}
    </div>
  );
}
