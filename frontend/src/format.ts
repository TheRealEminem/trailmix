export function formatDuration(sec: number): string {
  const s = Math.round(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const mm = String(m).padStart(2, "0");
  const rr = String(r).padStart(2, "0");
  return h ? `${h}:${mm}:${rr}` : `${m}:${rr}`;
}

export function formatBytes(n: number): string {
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

/** "mlx-community/whisper-large-v3-turbo" -> "whisper-large-v3-turbo" */
export const shortModel = (m: string) => m.split("/").pop() ?? m;

/** "/Users/you/Documents/x.md" -> "~/Documents/x.md" */
export const tildePath = (p: string) => p.replace(/^\/Users\/[^/]+/, "~");

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function formatLongDate(iso: string): string {
  const d = new Date(iso);
  const year = d.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year });
}

/** The local calendar day a meeting is on ("2026-10-14"): the sidebar groups by it. */
export function dayKey(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Sidebar group heading for a meeting date: "Today", "Yesterday", a weekday this past week, then the day itself
 * ("Oct 14", with the year when it isn't this one). */
export function dayGroup(iso: string, now = new Date()): string {
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const d = new Date(iso);
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
  const year = d.getFullYear() === now.getFullYear() ? undefined : "numeric";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year });
}

/** Newest first by when each meeting happened (imports keep their own date), whatever order they arrived in. */
export function newestFirst<T extends { id: number; created_at: string }>(items: T[]): T[] {
  return [...items].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id - a.id);
}
