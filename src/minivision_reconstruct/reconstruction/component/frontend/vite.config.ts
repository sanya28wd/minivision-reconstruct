import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    lib: { entry: { workspace: "src/index.ts", renderer: "src/renderer.ts" }, formats: ["es"], fileName: (_format: string, name: string): string => `${name}.js` },
    rollupOptions: { output: { assetFileNames: "workspace.[ext]" } },
  },
});
