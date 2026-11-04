import { describe, expect, it } from "vitest";
import { applyEvents, readEvents, subscribeEvents } from "./events";
import type { SystemInfo } from "./api";

const system = (free: number) => ({ ram_free_gb: free }) as unknown as SystemInfo;

describe("the events store", () => {
  it("keeps a part sent again unchanged, so what reads it doesn't draw again", () => {
    let changes = 0;
    const stop = subscribeEvents(() => changes++);
    applyEvents({ system: system(10) });
    const first = readEvents().system;
    applyEvents({ system: system(10) });
    expect(readEvents().system).toBe(first);
    expect(changes).toBe(1);
    applyEvents({ system: system(9) });
    expect(readEvents().system).not.toBe(first);
    expect(changes).toBe(2);
    stop();
  });

  it("leaves the other parts as they were", () => {
    const live = readEvents().live;
    applyEvents({ system: system(3) });
    expect(readEvents().live).toBe(live);
  });
});
