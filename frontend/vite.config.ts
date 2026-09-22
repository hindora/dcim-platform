import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Vite 5.4.12 added a Host-header check, and it rejects anything not
    // listed here with a bare "Blocked request. This host is not allowed."
    // That is served BEFORE any application code, so reaching this dev server
    // under any name but localhost looks dead rather than misconfigured.
    //
    // Configured, not hardcoded. Which hostnames are legitimate is a property
    // of where someone is running this, not of the product, and a specific
    // vendor's domain has no business being committed here. Set
    // VITE_ALLOWED_HOSTS to a comma-separated list; a leading dot matches
    // subdomains.
    //
    // Empty by default, which leaves Vite's own localhost-only behaviour in
    // place. Never `true`: the check exists to stop a remote page rebinding
    // DNS at this dev server, and switching it off wholesale would give that
    // up for every host rather than the intended one.
    //
    // Dev-server only. A real deployment serves the built assets from
    // `build.outDir` and this setting does nothing there.
    allowedHosts: (process.env.VITE_ALLOWED_HOSTS || '')
      .split(',').map((h) => h.trim()).filter(Boolean),
    // The API is proxied in development so the browser sees one origin and
    // there is no CORS or cookie-domain difference between dev and production.
    proxy: {
      '/api': {
        // deploy/docker-compose.yml already sets VITE_API_PROXY; honouring it
        // here is what makes that setting do anything, and it lets a developer
        // point the dev server at a backend on another port.
        target: process.env.VITE_API_PROXY || 'http://localhost:8000',
        changeOrigin: true,
        // Without this the /api/v1/ws upgrade is never forwarded: vite answers
        // the handshake itself, the socket closes, and the client retries for
        // ever behind a "live updates connecting" banner while every REST call
        // through the same proxy works fine. The banner is the only symptom,
        // which is why it reads as a backend fault and is not one.
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
