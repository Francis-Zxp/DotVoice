/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { resolve } from 'node:path';

// Two pages: the main window and the always-on-top overlay capsule.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  clearScreen: false,
  base: './',
  server: { port: 5174, strictPort: true },
  build: {
    target: 'chrome120',
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, 'index.html'),
        overlay: resolve(import.meta.dirname, 'overlay.html'),
        stage: resolve(import.meta.dirname, 'stage.html'),
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
