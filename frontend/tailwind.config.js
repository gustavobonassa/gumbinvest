/**
 * Design tokens for the app.
 *
 * The `series` scale is the validated categorical palette from the data-viz
 * guidelines (dark steps, checked against the #12141a chart surface: all eight
 * slots clear the lightness band, chroma floor, CVD separation and 3:1
 * contrast). Assign slots in order — never cycle, never recolour by rank.
 */
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        canvas: "#0b0d11",
        surface: {
          DEFAULT: "#12141a",
          raised: "#171a21",
          hover: "#1d212a",
        },
        line: {
          DEFAULT: "#232833",
          strong: "#2f3542",
        },
        ink: {
          DEFAULT: "#f4f6fb",
          secondary: "#a4adbf",
          muted: "#6c7689",
        },
        accent: {
          DEFAULT: "#3987e5",
          soft: "rgba(57,135,229,0.14)",
        },
        positive: "#199e70",
        negative: "#e66767",
        warning: "#c98500",
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
      boxShadow: {
        card: "0 1px 2px rgba(0,0,0,0.35), 0 8px 24px -12px rgba(0,0,0,0.6)",
        raised: "0 2px 4px rgba(0,0,0,0.4), 0 18px 40px -18px rgba(0,0,0,0.75)",
        glow: "0 0 0 1px rgba(57,135,229,0.35), 0 8px 30px -10px rgba(57,135,229,0.35)",
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
