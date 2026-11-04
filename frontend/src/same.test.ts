import { describe, expect, it } from "vitest";
import { same, sameItems } from "./same";

describe("same", () => {
  it("keeps the old value when the new one holds the same data", () => {
    const a = { n: 1, list: [1, 2] };
    expect(same(a, { n: 1, list: [1, 2] })).toBe(a);
    const b = { n: 2, list: [1, 2] };
    expect(same(a, b)).toBe(b);
  });
});

describe("sameItems", () => {
  const one = { id: 1, title: "One" };
  const two = { id: 2, title: "Two" };

  it("keeps the old list when nothing changed", () => {
    const prev = [one, two];
    expect(sameItems(prev, [{ id: 1, title: "One" }, { id: 2, title: "Two" }])).toBe(prev);
  });

  it("reuses the items that didn't change", () => {
    const next = sameItems([one, two], [{ id: 1, title: "One" }, { id: 2, title: "Two, renamed" }]);
    expect(next[0]).toBe(one);
    expect(next[1]).toEqual({ id: 2, title: "Two, renamed" });
  });

  it("notices items added, removed or reordered", () => {
    const prev = [one, two];
    expect(sameItems(prev, [{ id: 3, title: "Three" }, one, two])).toHaveLength(3);
    expect(sameItems(prev, [one])).toEqual([one]);
    const swapped = sameItems(prev, [{ id: 2, title: "Two" }, { id: 1, title: "One" }]);
    expect(swapped).not.toBe(prev);
    expect(swapped[0]).toBe(two);
  });
});
