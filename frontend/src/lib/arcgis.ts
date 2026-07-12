// Loads the ArcGIS Maps SDK for JavaScript (4.x) from the official CDN exactly once,
// then resolves the requested esri AMD modules via window.require.

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

function waitForRequire(timeoutMs: number): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      if (typeof window !== 'undefined' && window.require) { resolve(); return; }
      if (Date.now() - start > timeoutMs) {
        reject(new Error('ArcGIS loader did not initialise. Check the network connection.'));
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
    if (typeof window !== 'undefined' && window.require) { resolve(); return; }
    injectCss();

    const existing = document.querySelector(
      'script[data-arcgis="js"], script[src^="https://js.arcgis.com/"]'
    ) as HTMLScriptElement | null;
    if (existing) {
      existing.addEventListener('error', () =>
        reject(new Error('Could not load the ArcGIS Maps SDK. Check your network connection.'))
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
      reject(new Error('Could not load the ArcGIS Maps SDK. Check your network connection.'));
    document.head.appendChild(script);
  });

  return scriptPromise;
}

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

/** Premium ArcGIS basemaps are opt-in. */
export function usePremiumArcgisBasemaps(): boolean {
  return (import.meta.env.VITE_USE_PREMIUM_ARCGIS_BASEMAPS ?? '').trim() === '1';
}
