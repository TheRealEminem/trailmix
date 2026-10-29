import { describe, expect, test } from "vitest";
import { dayGroup, formatBytes, formatDuration, shortModel, tildePath } from "./format";

describe("formatDuration", () => {
  test("minutes and seconds, hours only when there are any", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(59)).toBe("0:59");
    expect(formatDuration(65.4)).toBe("1:05");
    expect(formatDuration(3600)).toBe("1:00:00");
    expect(formatDuration(3661)).toBe("1:01:01");
  });
});

describe("formatBytes", () => {
  test("never shows 0 KB, and picks the unit by size", () => {
    expect(formatBytes(10)).toBe("1 KB");
    expect(formatBytes(1536)).toBe("2 KB");
    expect(formatBytes(2.5 * 1024 ** 2)).toBe("2.5 MB");
    expect(formatBytes(3 * 1024 ** 3)).toBe("3.00 GB");
  });
});

test("shortModel drops the repository owner", () => {
  expect(shortModel("mlx-community/whisper-large-v3-turbo")).toBe("whisper-large-v3-turbo");
  expect(shortModel("large")).toBe("large");
});

test("tildePath shortens a home folder only", () => {
  expect(tildePath("/Users/dale/Documents/x.md")).toBe("~/Documents/x.md");
  expect(tildePath("/Volumes/Data/x.md")).toBe("/Volumes/Data/x.md");
});

describe("dayGroup", () => {
  const now = new Date(2026, 9, 29, 15, 0); // Thursday 29 October 2026
  const at = (y: number, m: number, d: number) => new Date(y, m - 1, d, 9).toISOString();

  test("today and yesterday by calendar day, not by 24 hours", () => {
    expect(dayGroup(at(2026, 10, 29), now)).toBe("Today");
    expect(dayGroup(new Date(2026, 9, 29, 23, 59).toISOString(), now)).toBe("Today");
    expect(dayGroup(at(2026, 10, 28), now)).toBe("Yesterday");
  });

  test("the weekday within a week, the month within the year, month and year before that", () => {
    expect(dayGroup(at(2026, 10, 26), now)).toBe(new Date(2026, 9, 26).toLocaleDateString(undefined, { weekday: "long" }));
    expect(dayGroup(at(2026, 10, 2), now)).toBe(new Date(2026, 9, 2).toLocaleDateString(undefined, { month: "long" }));
    expect(dayGroup(at(2025, 12, 24), now)).toBe(new Date(2025, 11, 24).toLocaleDateString(undefined, { month: "long", year: "numeric" }));
  });

  test("a date in the future counts as today", () => {
    expect(dayGroup(at(2026, 11, 1), now)).toBe("Today");
  });
});
