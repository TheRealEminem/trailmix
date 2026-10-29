import type { Settings } from "./api";

/** The home-screen checklist the guided setup replaced: closing it meant "I've done setup", so it still does. */
const OLD_CHECKLIST_CLOSED = "trailmix.setupDismissed";

/** Should a new install be walked through setup? Not once it's been done, skipped, or meetings exist. */
export function needsSetup(s: Settings, meetingCount: number): boolean {
  let closedChecklist = false;
  try {
    closedChecklist = localStorage.getItem(OLD_CHECKLIST_CLOSED) === "1";
  } catch {
    /* private window */
  }
  return !s.setup_done && meetingCount === 0 && !closedChecklist;
}
