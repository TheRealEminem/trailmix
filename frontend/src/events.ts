/**
 * What's going on, pushed by the server as it changes (GET /api/events, backend/events.py): the menu bar
 * recorder, recordings in progress, this Mac's memory and disk, and the speech models' downloads. One open
 * stream instead of each part of the window asking every couple of seconds. The browser reconnects by
 * itself, and the server sends everything again when it does.
 */
import { createContext, useEffect, useState } from "react";
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

export const EventsContext = createContext<Events>(NOTHING);

type Update = Partial<Pick<Events, "recorder" | "live" | "system" | "models">>;

export function useEvents(enabled: boolean): Events {
  const [events, setEvents] = useState<Events>(NOTHING);
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
      setEvents((cur) => ({ ...cur, ...changed, connected: true }));
    });
    source.onopen = () => setEvents((cur) => ({ ...cur, connected: true }));
    source.onerror = () => setEvents((cur) => ({ ...cur, connected: false }));
    return () => {
      source.close();
      setEvents((cur) => ({ ...cur, connected: false }));
    };
  }, [enabled]);
  return events;
}
