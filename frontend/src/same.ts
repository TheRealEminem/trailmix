/**
 * Polls bring back the same data most of the time. Keeping what's already there when nothing changed means
 * nothing that reads it draws again, and effects that depend on it don't run again (issue #14).
 */

/** `next`, or `prev` itself when it holds the same data. */
export function same<T>(prev: T, next: T): T {
  return prev === next || JSON.stringify(prev) === JSON.stringify(next) ? prev : next;
}

/** `next`, reusing each item of `prev` (by id) that hasn't changed; `prev` itself when nothing has. */
export function sameItems<T extends { id: number | string }>(prev: T[], next: T[]): T[] {
  const before = new Map(prev.map((item) => [item.id, item]));
  let changed = prev.length !== next.length;
  const out = next.map((item, i) => {
    const old = before.get(item.id);
    const kept = old !== undefined && JSON.stringify(old) === JSON.stringify(item) ? old : item;
    if (kept !== prev[i]) changed = true;
    return kept;
  });
  return changed ? out : prev;
}
