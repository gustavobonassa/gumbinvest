/**
 * Design tokens for the app.
 *
 * Every chrome colour resolves through a CSS variable rather than a literal, so
 * one `data-theme` on <html> repaints the app — see the two token blocks in
 * `styles.css`. Variables hold bare `R G B` channels precisely so Tailwind's
 * opacity modifiers (`bg-surface/80`, `border-line/70`) keep working.
 *
 * The `series` scale is the validated categorical palette from the data-viz
 * guidelines, and it is deliberately *not* themed: an asset class keeps one
 * colour everywhere, and the eight steps clear the lightness band, chroma
 * floor, CVD separation and contrast checks against both the dark (#12141a)
 * and the light (#ffffff) chart surface. Assign slots in order — never cycle,
 * never recolour by rank.
 */
const themed = (name) => `rgb(var(${name}) / <alpha-value>)`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        canvas: themed("--c-canvas"),
        surface: {
          DEFAULT: themed("--c-surface"),
          raised: themed("--c-surface-raised"),
          hover: themed("--c-surface-hover"),
        },
        line: {
          DEFAULT: themed("--c-line"),
          strong: themed("--c-line-strong"),
        },
        ink: {
          DEFAULT: themed("--c-ink"),
          secondary: themed("--c-ink-secondary"),
          muted: themed("--c-ink-muted"),
        },
        accent: {
          DEFAULT: themed("--c-accent"),
          // A fixed wash rather than a channel triplet: it is only ever used as
          // a background at its own alpha, never with an opacity modifier.
          soft: "var(--c-accent-soft)",
        },
        positive: themed("--c-positive"),
        negative: themed("--c-negative"),
        warning: themed("--c-warning"),
        series: {
          1: "#3987e5",
          2: "#d95926",
          3: "#199e70",
          4: "#c98500",
          5: "#d55181",
          6: "#008300",
          7: "#9085e9",
          8: "#e66767",
        },
      },
      fontFamily: {
        sans: ["Inter", "SF Pro Display", "Segoe UI", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "SF Mono", "Consolas", "monospace"],
      },
      borderRadius: { xl: "0.875rem", "2xl": "1.125rem", "3xl": "1.5rem" },
      // Themed as well: the depth that reads as elevation on a near-black page
      // reads as dirt on a white one, so each theme brings its own.
      boxShadow: {
        card: "var(--shadow-card)",
        raised: "var(--shadow-raised)",
        glow: "var(--shadow-glow)",
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": { "0%": { opacity: "0" }, "100%": { opacity: "1" } },
        shimmer: { "100%": { transform: "translateX(100%)" } },
        "scale-in": {
          "0%": { opacity: "0", transform: "scale(0.97)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
        "toast-in": {
          "0%": { opacity: "0", transform: "translateY(12px) scale(0.98)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        "toast-out": {
          "0%": { opacity: "1", transform: "translateX(0) scale(1)" },
          "100%": { opacity: "0", transform: "translateX(16px) scale(0.98)" },
        },
        "slide-in-right": {
          "0%": { transform: "translateX(100%)" },
          "100%": { transform: "translateX(0)" },
        },
        "slide-out-right": {
          "0%": { transform: "translateX(0)" },
          "100%": { transform: "translateX(100%)" },
        },
        "fade-out": { "0%": { opacity: "1" }, "100%": { opacity: "0" } },
      },
      animation: {
        "fade-up": "fade-up 0.45s cubic-bezier(0.22,1,0.36,1) both",
        "fade-in": "fade-in 0.3s ease both",
        "fade-out": "fade-out 0.24s ease both",
        "scale-in": "scale-in 0.18s cubic-bezier(0.22,1,0.36,1) both",
        "toast-in": "toast-in 0.22s cubic-bezier(0.22,1,0.36,1) both",
        "toast-out": "toast-out 0.18s ease both",
        // The chat panel: transform-only, so the slide composites on the GPU.
        // In/out share the premium curve; out is a touch quicker, as exits
        // should be.
        "chat-in": "slide-in-right 0.32s cubic-bezier(0.22,1,0.36,1) both",
        "chat-out": "slide-out-right 0.24s cubic-bezier(0.22,1,0.36,1) both",
      },
      transitionTimingFunction: { premium: "cubic-bezier(0.22,1,0.36,1)" },
    },
  },
  plugins: [],
};
