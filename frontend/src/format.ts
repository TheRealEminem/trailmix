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

/** "/Users/mark/Documents/x.md" -> "~/Documents/x.md" */
export const tildePath = (p: string) => p.replace(/^\/Users\/[^/]+/, "~");

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function formatLongDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

/** Sidebar group heading for a meeting date. */
export function dayGroup(iso: string, now = new Date()): string {
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const d = new Date(iso);
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString(undefined, { month: "long" });
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}
