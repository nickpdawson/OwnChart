import type { Config } from "tailwindcss";

// Design tokens are defined as CSS custom properties in `styles/tokens.css`
// so the design-skill output can replace them without touching this file.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "rgb(var(--oc-bg) / <alpha-value>)",
        surface: "rgb(var(--oc-surface) / <alpha-value>)",
        ink: "rgb(var(--oc-ink) / <alpha-value>)",
        muted: "rgb(var(--oc-muted) / <alpha-value>)",
        accent: "rgb(var(--oc-accent) / <alpha-value>)",
        evidence: "rgb(var(--oc-evidence) / <alpha-value>)",
        caution: "rgb(var(--oc-caution) / <alpha-value>)",
      },
      fontFamily: {
        sans: ["var(--oc-font-sans)", "system-ui", "sans-serif"],
        serif: ["var(--oc-font-serif)", "Georgia", "serif"],
        mono: ["var(--oc-font-mono)", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
