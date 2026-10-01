// Vite config for the Labzilla Console PWA. Built output (dist/) is served by lif.console.app.
// Dev: `npm run dev` proxies /api (including the /api/events SSE stream) to the console BFF on :8090.
import { defineConfig } from 'vite';
import preact from '@preact/preset-vite';

const BFF = 'http://127.0.0.1:8090';

export default defineConfig({
  plugins: [preact()],
  resolve: { alias: { '@': new URL('./src', import.meta.url).pathname } },
  build: {
    target: 'es2020',
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
    reportCompressedSize: true,          // budgets: initial JS ≤ 60 KB gz, Ask ≤ 75 KB gz, CSS ≤ 20 KB gz
    chunkSizeWarningLimit: 120,
    modulePreload: { polyfill: false },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    // changeOrigin stays false: the BFF's CSRF check compares Origin with Host, and both are :5173 here.
    // SSE streams through unbuffered: the BFF sends Cache-Control: no-cache and X-Accel-Buffering: no.
    proxy: {
      '/api': {
        target: BFF,
        changeOrigin: false,
      },
    },
  },
  preview: { host: '127.0.0.1', port: 4173, proxy: { '/api': { target: BFF, changeOrigin: false } } },
});
