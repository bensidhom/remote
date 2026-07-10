// frontend/vite.config.ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(() => {
  const backendUrl = process.env.VITE_BACKEND_URL || `http://127.0.0.1:${process.env.VITE_BACKEND_PORT || 5000}`

  return {
    plugins: [react()],
    server: {
      // Served on 8001. NOTE: the ArcGIS API key in .env.local is referrer-locked
      // to http://localhost:8000 — on 8001 it won't authenticate, so the map uses
      // free keyless basemaps (automatic fallback in ArcgisMap). To get the premium
      // ArcGIS basemaps, add http://localhost:8001 to the key's allowed referrers.
      port: 8001,
      strictPort: true,
      watch: {
        usePolling: true,        // This helps with EMFILE
        interval: 1000
      },
      proxy: {
        '/api': {
          target: backendUrl,
          changeOrigin: true,
          secure: false,
        }
      }
    }
  }
})