import { useEffect, useState } from "react";
import { api } from "../api";
import type { Health } from "../api";
import { since } from "../whatsNew";
import type { Release } from "../whatsNew";
import { CloseIcon, SparkleIcon } from "./icons";

/** Before "What's new" existed (0.14.0), so an update from then shows everything since. */
const BEFORE_WHATS_NEW = "0.11.0";

/**
 * Once after an update: the changes since the version you had, in plain words (whatsNew.ts). A new install
 * starts with nothing to tell. Closing it remembers the version.
 */
export default function WhatsNew({ health }: { health: Health | null }) {
  const [releases, setReleases] = useState<Release[]>([]);
  const version = health?.version;
  useEffect(() => {
    if (!version || version === "dev") return;
    Promise.all([api.getSettings(), api.listMeetings()])
      .then(([s, meetings]) => {
        // Never closed one: an update from before this card (if there are meetings), or a new install.
        const seen = s.whats_new_seen || (meetings.length ? BEFORE_WHATS_NEW : version);
        const fresh = since(seen, version);
        setReleases(fresh);
        if (!fresh.length && s.whats_new_seen !== version) void api.putSettings({ whats_new_seen: version });
      })
      .catch(() => undefined);
  }, [version]);
  if (!releases.length || !version) return null;
  const close = () => {
    setReleases([]);
    void api.putSettings({ whats_new_seen: version });
  };
  const items = releases.flatMap((r) => r.items);
  return (
    <section aria-label="What's new" className="glass mb-6 animate-enter px-5 py-4 sm:px-6">
      <header className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <span className="well well-forest">
            <SparkleIcon size={16} />
          </span>
          <div>
            <h2 className="font-display text-lead font-semibold tracking-[-0.01em]">What's new in Trailmix {version}</h2>
            {releases.length > 1 && (
              <p className="text-hint text-ink-soft">Everything since {releases[releases.length - 1].version.replace(/\.0$/, "")} and before.</p>
            )}
          </div>
        </div>
        <button onClick={close} className="icon-btn h-8 w-8" aria-label="Close what's new" data-tip="Close">
          <CloseIcon size={16} />
        </button>
      </header>
      <ul className="mt-3 grid gap-x-6 gap-y-3 sm:grid-cols-2">
        {items.map((i) => (
          <li key={i.title} className="text-hint leading-relaxed text-ink-soft">
            <span className="block text-label font-medium text-ink">{i.title}</span>
            {i.body}
            {i.where && <span className="mt-0.5 block text-meta text-forest">{i.where}</span>}
          </li>
        ))}
      </ul>
      <div className="mt-4">
        <button className="btn btn-sm btn-soft" onClick={close}>
          Got it
        </button>
      </div>
    </section>
  );
}
