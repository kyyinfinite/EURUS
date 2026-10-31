/** @type {import('tailwindcss').Config} */
/*
 * EURUS light design system — "warm enterprise".
 *
 * The palette is fixed by the EURUS 2.0 design brief:
 *   canvas #F7F5F1 · surface #FFFFFF · secondary surface #EDE7DF
 *   copper #995329 (brand) · copper-dark #713B20
 *   ink #332A24 · muted #81766D · line #E8E1D9 · success #278866
 * Copper/brown is the only brand accent; no purple, no neon, no dark theme.
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#F7F5F1",
        surface: "#FFFFFF",
        raised: "#EDE7DF", // secondary surface (chips, wells, hover fills)
        "raised-soft": "#F4F0EA",
        ink: "#332A24",
        dim: "#5C534B", // supporting text (darker than muted for readability)
        muted: "#81766D", // secondary text
        faint: "#A29A91", // hints, timestamps
        line: "#E8E1D9", // borders
        "line-soft": "#F0EBE4", // subtle dividers
        copper: {
          DEFAULT: "#995329", // primary copper
          dark: "#713B20", // pressed / strong text on light
          deep: "#5E2F1A", // darkest copper (headings accents)
          soft: "#F6EDE5", // copper-tinted fill (hover wells)
          tint: "#FBF6F1", // faintest copper wash
        },
        bronze: "#B0703F", // secondary warm accent (sparingly)
        ok: "#278866",
        "ok-soft": "#E7F3EE",
        warn: "#B45309",
        "warn-soft": "#FBF1E4",
        danger: "#B3261E",
        "danger-soft": "#FBEDEB",
        info: "#4A6B8A",
        "info-soft": "#EDF2F7",
        sage: "#56776A", // external-origin / system green-gray
        "sage-soft": "#EAF1EE",
      },
      fontFamily: {
        sans: '"Inter", "Inter var", "Segoe UI", system-ui, -apple-system, sans-serif',
        mono: '"JetBrains Mono", "Fira Code", ui-monospace, "SF Mono", Menlo, monospace',
      },
      fontSize: {
        // Micro type is deliberately constrained: 11px only for tiny mono
        // metadata; body copy stays 13–15px (brief: no 2xs/3xs sprawl).
        "2xs": ["0.6875rem", { lineHeight: "1rem", fontWeight: "500" }],
        meta: ["0.8125rem", { lineHeight: "1.25rem" }],
      },
      borderRadius: {
        DEFAULT: "8px",
        sm: "6px",
        md: "10px",
        lg: "12px",
        xl: "14px",
        "2xl": "18px",
      },
      boxShadow: {
        panel: "0 1px 2px rgba(51, 42, 36, 0.05)",
        card: "0 1px 3px rgba(51, 42, 36, 0.06), 0 1px 2px rgba(51, 42, 36, 0.04)",
        float:
          "0 4px 16px rgba(51, 42, 36, 0.10), 0 1px 3px rgba(51, 42, 36, 0.06)",
        overlay:
          "0 24px 56px rgba(51, 42, 36, 0.18), 0 4px 12px rgba(51, 42, 36, 0.08)",
        launcher: "0 6px 20px rgba(115, 59, 32, 0.28)",
      },
      transitionTimingFunction: {
        out: "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      animation: {
        "spin-slow": "spin 2.5s linear infinite",
        "fade-in": "fade-in 0.18s ease-out",
        "fade-out": "fade-out 0.15s ease-in",
        "scale-in": "scale-in 0.2s cubic-bezier(0.16, 1, 0.3, 1)",
        "slide-up": "slide-up 0.22s cubic-bezier(0.16, 1, 0.3, 1)",
        "slide-in-right": "slide-in-right 0.22s cubic-bezier(0.16, 1, 0.3, 1)",
        "slide-in-left": "slide-in-left 0.22s cubic-bezier(0.16, 1, 0.3, 1)",
        "toast-in": "toast-in 0.22s cubic-bezier(0.16, 1, 0.3, 1)",
        "pulse-soft": "pulse-soft 2.4s ease-in-out infinite",
        "slide-up-sheet": "slide-up-sheet 0.28s cubic-bezier(0.16, 1, 0.3, 1)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "fade-out": {
          from: { opacity: "1" },
          to: { opacity: "0" },
        },
        "scale-in": {
          from: { opacity: "0", transform: "scale(0.96) translateY(4px)" },
          to: { opacity: "1", transform: "scale(1) translateY(0)" },
        },
        "slide-up": {
          from: { opacity: "0", transform: "translateY(12px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "slide-in-right": {
          from: { opacity: "0", transform: "translateX(24px)" },
          to: { opacity: "1", transform: "translateX(0)" },
        },
        "slide-in-left": {
          from: { opacity: "0", transform: "translateX(-24px)" },
          to: { opacity: "1", transform: "translateX(0)" },
        },
        "toast-in": {
          from: { opacity: "0", transform: "translateY(10px) scale(0.97)" },
          to: { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        "slide-up-sheet": {
          from: { opacity: "0.4", transform: "translateY(48px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "pulse-soft": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.45" },
        },
      },
      screens: {
        xs: "420px",
      },
    },
  },
  plugins: [],
};
