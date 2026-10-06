import { useState } from "react";
import { api } from "../api";
import type { NativeRecorder } from "../api";
import { AlertIcon, DownloadIcon } from "./icons";
import { Spinner } from "./ui";

/** "Trailmix 0.4.0 is available": one click downloads, checks and installs it, then Trailmix restarts. */
export default function UpdateBanner({
  native,
}: {
  native: NativeRecorder | null;
}) {
  const [error, setError] = useState<string | null>(null);
  const update = native?.update;
  if (!update) return null;
  const busy = update.state === "downloading" || update.state === "installing";
  const go = () => {
    setError(null);
    api.installUpdate().catch((e) => setError((e as Error).message));
  };

  return (
    <div
      role="status"
      className="mb-6 flex animate-enter flex-wrap items-center gap-3 rounded-lg border border-forest/20 bg-forest/[.06] px-4 py-3 text-label"
    >
      {update.state === "failed" ? (
        <AlertIcon size={18} className="shrink-0 text-trail-deep" />
      ) : (
        <DownloadIcon size={18} className="shrink-0 text-forest" />
      )}
      <div className="min-w-0 flex-1">
        {update.state === "available" && (
          <>
            <span className="font-semibold">
              Trailmix {update.version} is available.
            </span>{" "}
            <span className="text-ink-soft">
              It installs in about a minute and Trailmix restarts. macOS will
              ask for the microphone and system audio again afterwards.
            </span>
          </>
        )}
        {update.state === "downloading" && (
          <>
            Downloading Trailmix {update.version}
            {update.progress != null
              ? ` · ${Math.round(update.progress * 100)}%`
              : "…"}
          </>
        )}
        {update.state === "installing" && (
          <>Installing Trailmix {update.version}. It restarts in a moment…</>
        )}
        {update.state === "failed" && (
          <span className="text-trail-deep">
            Couldn't update: {update.error}
          </span>
        )}
        {error && <div className="text-trail-deep">{error}</div>}
      </div>
      {busy ? (
        <Spinner />
      ) : (
        <div className="flex shrink-0 gap-2">
          {update.notes && (
            <a className="btn btn-sm btn-ghost" href={update.notes}>
              What's new
            </a>
          )}
          <button className="btn btn-sm btn-primary" onClick={go}>
            {update.state === "failed" ? "Try again" : "Update now"}
          </button>
        </div>
      )}
    </div>
  );
}
