import { defineConfig } from "vite";

export default defineConfig({
  root: ".",
  base: "./",
  build: {
    outDir: "dist",
    target: "es2022",
    assetsInlineLimit: 0,
  },
  server: {
    port: 5173,
    strictPort: false,
  },
});
