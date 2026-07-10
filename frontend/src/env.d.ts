// Ambient type declarations for Vite env vars and the ArcGIS AMD loader.
// (No `vite/client` reference in tsconfig, so we declare what we use here.)

interface ImportMetaEnv {
  /** Optional ArcGIS API key. Only used for premium basemaps when explicitly enabled. */
  readonly VITE_ARCGIS_API_KEY?: string;
  /** Set to "1" to use premium ArcGIS basemaps instead of public keyless basemaps. */
  readonly VITE_USE_PREMIUM_ARCGIS_BASEMAPS?: string;
  readonly VITE_BACKEND_URL?: string;
  readonly VITE_BACKEND_PORT?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

interface Window {
  /** Injected by the ArcGIS Maps SDK CDN bundle (js.arcgis.com/<version>/). */
  require?: (
    modules: string[],
    callback: (...modules: any[]) => void,
    errback?: (err: any) => void
  ) => void;
}
