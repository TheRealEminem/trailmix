export interface Env {
  DB: D1Database;
  /** Where people reach this Worker, e.g. https://cloud.trailmix.app */
  BASE_URL: string;
  /** Google's API, overridable for tests. */
  GEMINI_BASE?: string;
  GEMINI_API_KEY: string;
  LEMONSQUEEZY_SIGNING_SECRET: string;
  /** JSON: Lemon Squeezy variant id → plan ("basic" | "pro" | "max"). */
  LEMONSQUEEZY_VARIANTS?: string;
  /** JSON: plan → Lemon Squeezy checkout link. */
  CHECKOUT_URLS?: string;
  RESEND_API_KEY?: string;
  EMAIL_FROM?: string;
  /** JSON merged over the defaults in config.ts. */
  CLOUD_CONFIG?: string;
  /** "1": print sign-in links to the console instead of emailing them. */
  DEV_MODE?: string;
}
