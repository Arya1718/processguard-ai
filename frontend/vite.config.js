import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Only VITE_-prefixed variables are exposed to the client (see .env.example).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Dev parity with the container: `npm run dev` proxies /api to the
    // middleware so the app is always same-origin (the deployed nginx image
    // does the same via nginx.conf). No CORS anywhere.
    proxy: {
      '/api': {
        target: process.env.PGAI_API_TARGET || 'http://localhost:8080',
        changeOrigin: true,
      },
      // Prompt 7: dev parity with nginx -- the provider is also reachable
      // from the dev server under /oidc (single-origin browser flow).
      '/oidc': {
        target: process.env.PGAI_OIDC_TARGET || 'http://localhost:8090',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/oidc/, ''),
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.js',
    css: false,
    // Component tests only -- the Playwright e2e specs (e2e/*.spec.js) are
    // run separately via `npm run e2e` and must not be collected here.
    include: ['src/**/*.{test,spec}.{js,jsx}'],
  },
})
