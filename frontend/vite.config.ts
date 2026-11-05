import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8712",
        changeOrigin: false,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules")) {
            if (id.includes("recharts") || id.includes("d3-") || id.includes("victory-vendor")) {
              return "charts";
            }
            if (
              id.includes("react-markdown") ||
              id.includes("remark") ||
              id.includes("rehype") ||
              id.includes("micromark") ||
              id.includes("mdast") ||
              id.includes("hast") ||
              id.includes("katex") ||
              id.includes("unified") ||
              id.includes("unist")
            ) {
              return "markdown";
            }
            if (
              id.includes("react-dom") ||
              id.includes("react/") ||
              id.includes("scheduler") ||
              id.includes("@remix-run") ||
              id.includes("react-router")
            ) {
              return "react-vendor";
            }
          }
          return undefined;
        },
      },
    },
  },
});
