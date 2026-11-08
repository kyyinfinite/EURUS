export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#F7F5F1",
        surface: "#FFFFFF",
        raised: "#EDE7DF",
        "raised-soft": "#F4F0EA",
        ink: "#332A24",
        dim: "#5C534B",
        muted: "#81766D",
        faint: "#A29A91",
        line: "#E8E1D9",
        "line-soft": "#F0EBE4",
        copper: {
          DEFAULT: "#995329",
          dark: "#713B20",
          deep: "#5E2F1A",
          soft: "#F6EDE5",
          tint: "#FBF6F1",
        },
        bronze: {
          DEFAULT: "#B0703F",
          dark: "#8A5430",
          soft: "#F4E8DD",
        },
        tech: {
          DEFAULT: "#2B241E",
          soft: "#352D26",
          line: "#463C33",
          ink: "#EFE7DD",
          dim: "#B4A696",
        },
        ok: "#278866",
        "ok-soft": "#E7F3EE",
        warn: "#B45309",
        "warn-soft": "#FBF1E4",
        danger: "#B3261E",
        "danger-soft": "#FBEDEB",
        info: "#4A6B8A",
        "info-soft": "#EDF2F7",
        sage: "#56776A",
        "sage-soft": "#EAF1EE",
      },
      fontFamily: {
        sans: '"Inter", "Inter var", "Segoe UI", system-ui, -apple-system, sans-serif',
        mono: '"JetBrains Mono", "Fira Code", ui-monospace, "SF Mono", Menlo, monospace',
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem", fontWeight: "500" }],
        "3xs": ["0.625rem", { lineHeight: "0.875rem" }],
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
        card: "0 1px 2px rgba(51, 42, 36, 0.05), 0 2px 10px rgba(51, 42, 36, 0.05), inset 0 1px 0 rgba(255, 255, 255, 0.72)",
        raised:
          "0 1px 2px rgba(51, 42, 36, 0.06), 0 3px 14px rgba(51, 42, 36, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.8)",
        well: "inset 0 1px 3px rgba(51, 42, 36, 0.08), inset 0 0 0 1px rgba(51, 42, 36, 0.045)",
        float:
          "0 4px 16px rgba(51, 42, 36, 0.10), 0 1px 3px rgba(51, 42, 36, 0.06), inset 0 1px 0 rgba(255, 255, 255, 0.82)",
        pop: "0 8px 28px rgba(51, 42, 36, 0.14), 0 2px 8px rgba(51, 42, 36, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.85)",
        overlay:
          "0 24px 56px rgba(51, 42, 36, 0.18), 0 4px 12px rgba(51, 42, 36, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.6)",
        launcher: "0 6px 20px rgba(115, 59, 32, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.25)",
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
        shimmer: "shimmer 1.8s ease-in-out infinite",
        breathe: "breathe 2.2s ease-in-out infinite",
        "float-soft": "float-soft 7s ease-in-out infinite",
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
        shimmer: {
          "0%": { backgroundPosition: "-160px 0" },
          "100%": { backgroundPosition: "260px 0" },
        },
        breathe: {
          "0%, 100%": { transform: "scale(1)", opacity: "0.55" },
          "50%": { transform: "scale(1.35)", opacity: "0" },
        },
        "float-soft": {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-9px)" },
        },
      },
      screens: {
        xs: "420px",
      },
    },
  },
  plugins: [],
}
