/** @type {import('tailwindcss').Config} */

// Every color, radius and shadow is a CSS variable defined in src/index.css (light "Day" and dark "Dusk").
// Colors are mixed with transparent so Tailwind's opacity modifiers keep working (bg-forest/10).
const c = (name) => `color-mix(in srgb, var(--${name}) calc(<alpha-value> * 100%), transparent)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    // Replaced, not extended: only the design-system radii and shadows exist.
    borderRadius: {
      none: "0",
      xs: "var(--radius-xs)",
      sm: "var(--radius-sm)",
      DEFAULT: "var(--radius-sm)",
      md: "var(--radius-md)",
      lg: "var(--radius-lg)",
      xl: "var(--radius-xl)",
      hero: "var(--radius-hero)",
      full: "9999px",
    },
    boxShadow: {
      none: "none",
      sm: "var(--shadow-sm)",
      DEFAULT: "var(--shadow-sm)",
      md: "var(--shadow-md)",
      lg: "var(--shadow-lg)",
      panel: "var(--shadow-panel)",
      chrome: "var(--shadow-chrome)",
      focus: "var(--ring-focus)",
    },
    extend: {
      colors: {
        canvas: c("bg"),
        glass: "var(--surface)",
        surface: { DEFAULT: c("surface-solid"), subtle: c("surface-subtle") },
        ink: { DEFAULT: c("text-primary"), soft: c("text-secondary"), faint: c("text-tertiary") },
        line: { DEFAULT: "var(--border)", strong: "var(--border-strong)" },
        forest: { DEFAULT: c("forest"), hover: c("forest-hover"), soft: c("forest-soft"), deep: c("forest-ink") },
        trail: { DEFAULT: c("trail-orange"), hover: c("trail-orange-hover"), soft: c("trail-orange-soft"), deep: c("trail-orange-ink") },
        sun: { DEFAULT: c("yellow"), soft: c("yellow-soft"), deep: c("yellow-ink") },
        sky: { DEFAULT: c("sky"), soft: c("sky-soft"), deep: c("sky-ink") },
        berry: { DEFAULT: c("berry"), soft: c("berry-soft"), deep: c("berry-ink") },
        "on-accent": c("on-accent"),
      },
      fontSize: {
        caption: "var(--fs-caption)",
        meta: "var(--fs-meta)",
        hint: "var(--fs-hint)",
        ui: "var(--fs-ui)",
        label: "var(--fs-label)",
        body: "var(--fs-body)",
        heading: "var(--fs-heading)",
        lead: "var(--fs-lead)",
        wordmark: "var(--fs-wordmark)",
      },
      fontFamily: {
        sans: ['"Geist Variable"', "-apple-system", "BlinkMacSystemFont", '"SF Pro Text"', "system-ui", "sans-serif"],
        display: ['"Fraunces Variable"', "ui-serif", "Georgia", "serif"],
        mono: ['"Geist Mono Variable"', "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      transitionTimingFunction: {
        out: "var(--ease-out)",
        spring: "var(--ease-spring)",
      },
      transitionDuration: {
        fast: "140ms",
        DEFAULT: "180ms",
      },
      keyframes: {
        enter: { from: { opacity: "0", transform: "translateY(8px)" }, to: { opacity: "1", transform: "none" } },
        "fade-in": { from: { opacity: "0" }, to: { opacity: "1" } },
        pop: { from: { opacity: "0", transform: "translateY(-4px) scale(0.98)" }, to: { opacity: "1", transform: "none" } },
        "dialog-in": { from: { opacity: "0", transform: "translateY(6px) scale(0.98)" }, to: { opacity: "1", transform: "none" } },
        "soft-pulse": { "0%, 100%": { opacity: "1" }, "50%": { opacity: "0.4" } },
        "ring-pulse": {
          "0%": { transform: "scale(0.9)", opacity: "0.55" },
          "100%": { transform: "scale(2.4)", opacity: "0" },
        },
        shimmer: { from: { backgroundPosition: "150% 0" }, to: { backgroundPosition: "-50% 0" } },
        "trail-walk": { to: { strokeDashoffset: "-24" } },
        "check-pop": { "0%": { transform: "scale(0.6)" }, "60%": { transform: "scale(1.12)" }, "100%": { transform: "scale(1)" } },
      },
      animation: {
        enter: "enter 480ms var(--ease-out) both",
        "fade-in": "fade-in 200ms var(--ease-out) both",
        pop: "pop 150ms var(--ease-out) both",
        "dialog-in": "dialog-in 200ms var(--ease-out) both",
        "soft-pulse": "soft-pulse 2s ease-in-out infinite",
        "ring-pulse": "ring-pulse 2s var(--ease-out) infinite",
        shimmer: "shimmer 1.8s linear infinite",
        "trail-walk": "trail-walk 1.4s linear infinite",
        "check-pop": "check-pop 260ms var(--ease-spring) both",
      },
    },
  },
  plugins: [],
};
