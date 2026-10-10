import { useEffect, useState } from "react";
import { api, PRIVACY_POLICY } from "../api";
import type { Settings } from "../api";
import { ChartIcon } from "./icons";

/**
 * The usage-stats choice, the first thing on the home screen (new installs, and once after the update that
 * brought stats). The box starts ticked; nothing is sent until Continue. Settings → Privacy changes it later.
 */
export default function StatsNotice() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [available, setAvailable] = useState(false);
  const [share, setShare] = useState(true);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    Promise.all([api.getSettings(), api.stats()])
      .then(([s, st]) => {
        setSettings(s);
        setShare(s.share_stats);
        setAvailable(st.available);
      })
      .catch(() => undefined);
  }, []);
  if (!settings || !available || settings.stats_notice_seen) return null;
  const go = () => {
    setBusy(true);
    api
      .putSettings({ share_stats: share, stats_notice_seen: true })
      .then(setSettings)
      .finally(() => setBusy(false));
  };
  return (
    <section aria-label="Usage stats" className="glass mb-6 animate-enter px-5 py-4 sm:px-6">
      <div className="flex items-start gap-3.5">
        <span className="well well-forest mt-0.5">
          <ChartIcon size={16} />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="font-display text-lead font-semibold tracking-[-0.01em]">Help Trailmix work on more computers</h2>
          <p className="mt-1 text-hint leading-relaxed text-ink-soft">
            Trailmix is free. To know which computers and set-ups to support, it can send a few anonymous stats
            once a day: the app version, your kind of computer (chip, memory, disk space), which features are on
            and how long processing takes. <b className="font-medium text-ink">Never anything from your meetings</b>,
            your name or your files. You can turn it off here or in Settings → Privacy at any time.
          </p>
          <label className="mt-3 flex cursor-pointer items-center gap-2.5 text-label">
            <input
              type="checkbox"
              className="h-4 w-4 accent-[var(--forest)]"
              checked={share}
              onChange={(e) => setShare(e.target.checked)}
            />
            Share anonymous usage stats
          </label>
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
            <button className="btn btn-sm btn-primary" onClick={go} disabled={busy}>
              Continue
            </button>
            <span className="text-hint text-ink-soft">
              By continuing you agree to the{" "}
              <a className="text-forest underline decoration-forest/30 underline-offset-2" href={PRIVACY_POLICY} target="_blank" rel="noreferrer">
                privacy policy
              </a>
              .
            </span>
          </div>
        </div>
      </div>
    </section>
  );
}
