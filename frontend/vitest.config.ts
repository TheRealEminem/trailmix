// Unit tests for the web app's plain modules (formatting, notes markdown, spend warnings, the problem report's
// screen text): `npm test`. Files that need a DOM say so with a `@vitest-environment jsdom` docblock.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["src/**/*.test.ts"],
    environment: "node",
  },
});
