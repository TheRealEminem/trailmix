/**
 * What's going on, pushed by the server as it changes (GET /api/events, backend/events.py): the menu bar
 * recorder, recordings in progress, this Mac's memory and disk, and the speech models' downloads. One open
 * stream instead of each part of the window asking every couple of seconds. The browser reconnects by
 * itself, and the server sends everything again when it does.
 *
 * Kept outside React, and read a part at a time (`useEvent`): the server sends the memory readout every couple
 * of seconds, and only the parts showing it should draw again, not the whole window (issue #14).
 */
import { useEffect, useSyncExternalStore } from "react";
import type { LiveRecording, NativeRecorder, SpeechModels, SystemInfo } from "./api";

export interface Events {
  /** The menu bar recorder, if it's in touch with the server. */
  recorder: NativeRecorder | null;
  /** Recordings in progress, whichever app is capturing them. */
  live: LiveRecording[];
  system: SystemInfo | null;
  models: SpeechModels | null;
  /** The stream is open. While it isn't, the values above are the last ones heard. */
  connected: boolean;
}

const NOTHING: Events = { recorder: null, live: [], system: null, models: null, connected: false };

type Update = Partial<Pick<Events, "recorder" | "live" | "system" | "models">>;

let current: Events = NOTHING;
const listeners = new Set<() => void>();

/** Takes in what changed. A part sent again as it was keeps the value it had, so nothing reading it draws again. */
export function applyEvents(changed: Partial<Events>): void {
  let next = current;
  for (const key of Object.keys(changed) as (keyof Events)[]) {
    const value = changed[key];
    if (value === undefined || value === current[key]) continue;
    if (typeof value === "object" && value !== null && JSON.stringify(value) === JSON.stringify(current[key])) continue;
    next = { ...next, [key]: value };
  }
  if (next === current) return;
  current = next;
  listeners.forEach((l) => l());
}

/** Calls `listener` whenever any part changes; returns how to stop. */
export function subscribeEvents(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Everything heard so far. */
export function readEvents(): Events {
  return current;
}

/** One part of the stream: draws again only when that part changes. */
export function useEvent<K extends keyof Events>(key: K): Events[K] {
  return useSyncExternalStore(subscribeEvents, () => current[key]);
}

/** Opens the stream while `enabled` (signed in). Once, at the top of the app. */
export function useEventStream(enabled: boolean): void {
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    const source = new EventSource("/api/events");
    source.addEventListener("state", (e) => {
      let changed: Update;
      try {
        changed = JSON.parse((e as MessageEvent<string>).data) as Update;
      } catch {
        return;
      }
      applyEvents({ ...changed, connected: true });
    });
    source.onopen = () => applyEvents({ connected: true });
    source.onerror = () => applyEvents({ connected: false });
    return () => {
      source.close();
      applyEvents({ connected: false });
    };
  }, [enabled]);
}
