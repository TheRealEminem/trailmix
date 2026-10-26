import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig(async () => {
  const migrations = await readD1Migrations("./migrations");
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          bindings: {
            TEST_MIGRATIONS: migrations,
            BASE_URL: "https://cloud.test",
            GEMINI_BASE: "https://google.test",
            GEMINI_API_KEY: "google-key",
            LEMONSQUEEZY_SIGNING_SECRET: "ls-secret",
            LEMONSQUEEZY_VARIANTS: JSON.stringify({ "101": "basic", "102": "pro", "103": "max" }),
            CHECKOUT_URLS: JSON.stringify({ pro: "https://shop.test/buy/pro", max: "https://shop.test/buy/max" }),
            DEV_MODE: "1",
          },
        },
      }),
    ],
    test: { setupFiles: ["./test/setup.ts"] },
  };
});
