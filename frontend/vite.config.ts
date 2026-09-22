import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Vite 5.4.12 added a Host-header check, and it rejects anything not
    // listed here with a bare "Blocked request. This host is not allowed."
    // That is served BEFORE any application code, so a tunnelled DCIM looks
    // dead rather than misconfigured. A leading dot matches subdomains, which
    // is what makes an ephemeral ngrok hostname work without editing this
    // file every time the tunnel restarts.
    //
    // Listed explicitly rather than `true`: the check exists to stop a remote
    // page rebinding DNS at this dev server, and switching it off wholesale
    // would give that up for every host, not just the tunnel's.
    allowedHosts: ['.ngrok-free.app', '.ngrok.app', '.ngrok-free.dev'],
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
