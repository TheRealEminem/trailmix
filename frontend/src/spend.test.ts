// @vitest-environment jsdom
import { expect, test } from "vitest";
import { isBigSpend } from "./spend";

test("a cost at or over the threshold is big; under it isn't", () => {
  expect(isBigSpend(0.5, false, 50)).toBe(true);
  expect(isBigSpend(0.49, false, 50)).toBe(false);
  expect(isBigSpend(0, false, 50)).toBe(false);
});

test("an unknown price only counts for bulk work", () => {
  expect(isBigSpend(null, false, 50)).toBe(false);
  expect(isBigSpend(undefined, true, 50)).toBe(true);
});

test("a threshold of 0 means never ask", () => {
  expect(isBigSpend(100, true, 0)).toBe(false);
  expect(isBigSpend(null, true, 0)).toBe(false);
});
