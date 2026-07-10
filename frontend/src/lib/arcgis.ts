// Loads the ArcGIS Maps SDK for JavaScript (4.x) from the official CDN exactly once,
// then resolves the requested esri AMD modules via window.require.
//
// This mirrors the integration pattern used by the Turf_Maintenance reference project
// (js.arcgis.com + window.require + esriConfig.apiKey). Using the CDN keeps the Electron
// bundle small and avoids the heavy @arcgis/core install, while still giving full SDK power.

const ARCGIS_VERSION = '4.31';
const CSS_HREF = `https://js.arcgis.com/${ARCGIS_VERSION}/esri/themes/light/main.css`;
const SCRIPT_SRC = `https://js.arcgis.com/${ARCGIS_VERSION}/`;

let scriptPromise: Promise<void> | null = null;

function injectCss(): void {
  if (document.querySelector('link[data-arcgis="css"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = CSS_HREF;
  link.dataset.arcgis = 'css';
  document.head.appendChild(link);
}

// Resolves once window.require (the Dojo AMD loader) is available, polling for
// up to `timeoutMs` to absorb any gap between <script> onload and loader init.
function waitForRequire(timeoutMs: number): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      if (typeof window !== 'undefined' && window.require) { resolve(); return; }
      if (Date.now() - start > timeoutMs) {
        reject(new Error('ArcGIS loader did not initialise (window.require unavailable). Check the network / CDN.'));
        return;
      }
      setTimeout(tick, 100);
    };
    tick();
  });
}

function loadScript(): Promise<void> {
  if (scriptPromise) return scriptPromise;

  scriptPromise = new Promise<void>((resolve, reject) => {
    // Already initialised (static <script> in index.html, or a prior load).
    if (typeof window !== 'undefined' && window.require) { resolve(); return; }

    injectCss();

    // A script tag may already exist (static include from index.html still loading).
    const existing = document.querySelector(
      'script[data-arcgis="js"], script[src^="https://js.arcgis.com/"]'
    ) as HTMLScriptElement | null;

    if (existing) {
      existing.addEventListener('error', () =>
        reject(new Error('Could not load the ArcGIS Maps SDK from the CDN. Check your network connection.'))
      );
      waitForRequire(25000).then(resolve, reject);
      return;
    }

    const script = document.createElement('script');
    script.src = SCRIPT_SRC;
    script.async = true;
    script.dataset.arcgis = 'js';
    script.onload = () => waitForRequire(25000).then(resolve, reject);
    script.onerror = () =>
      reject(new Error('Could not load the ArcGIS Maps SDK from the CDN. Check your network connection.'));
    document.head.appendChild(script);
  });

  return scriptPromise;
}

/**
 * Loads the ArcGIS SDK (once) and resolves the requested esri modules in order.
 *
 * @example
 * const [Map, MapView] = await loadEsriModules(['esri/Map', 'esri/views/MapView']);
 */
export async function loadEsriModules<T extends unknown[] = any[]>(
  modules: string[]
): Promise<T> {
  await loadScript();
  return new Promise<T>((resolve, reject) => {
    const req = window.require;
    if (!req) {
      reject(new Error('ArcGIS loader (window.require) is unavailable.'));
      return;
    }
    req(
      modules,
      (...mods: any[]) => resolve(mods as unknown as T),
      (err: any) => reject(err)
    );
  });
}

/** Optional ArcGIS API key from Vite env. Empty string when not configured. */
export function getArcgisApiKey(): string {
  return (import.meta.env.VITE_ARCGIS_API_KEY ?? '').trim();
}

/** Premium ArcGIS basemaps can trigger the SDK sign-in dialog when a key is invalid/referrer-blocked. */
export function usePremiumArcgisBasemaps(): boolean {
  return (import.meta.env.VITE_USE_PREMIUM_ARCGIS_BASEMAPS ?? '').trim() === '1';
}
