// Bundle the ArcGIS Maps SDK with the application instead of loading its AMD
// runtime from js.arcgis.com. Basemap tiles and hosted feature layers still use
// their normal network services, but SDK startup is local and keyless.
import '@arcgis/core/assets/esri/themes/light/main.css';

const LOCAL_MODULES: Record<string, () => Promise<{ default: unknown }>> = {
  'esri/config': () => import('@arcgis/core/config.js'),
  'esri/Map': () => import('@arcgis/core/Map.js'),
  'esri/views/MapView': () => import('@arcgis/core/views/MapView.js'),
  'esri/layers/FeatureLayer': () => import('@arcgis/core/layers/FeatureLayer.js'),
  'esri/Graphic': () => import('@arcgis/core/Graphic.js'),
  'esri/renderers/HeatmapRenderer': () => import('@arcgis/core/renderers/HeatmapRenderer.js'),
  'esri/renderers/ClassBreaksRenderer': () => import('@arcgis/core/renderers/ClassBreaksRenderer.js'),
  'esri/symbols/SimpleMarkerSymbol': () => import('@arcgis/core/symbols/SimpleMarkerSymbol.js'),
};

/** Load the requested SDK modules from the local application bundle. */
export async function loadEsriModules<T extends unknown[] = any[]>(
  modules: string[]
): Promise<T> {
  const loaders = modules.map((name) => {
    const loader = LOCAL_MODULES[name];
    if (!loader) throw new Error(`Unsupported local ArcGIS module: ${name}`);
    return loader();
  });
  const loaded = await Promise.all(loaders);
  return loaded.map((module) => module.default) as T;
}

/** Optional ArcGIS API key from Vite env. Empty string when not configured. */
export function getArcgisApiKey(): string {
  return (import.meta.env.VITE_ARCGIS_API_KEY ?? '').trim();
}

/** Premium ArcGIS basemaps are opt-in; keyless basemaps remain the default. */
export function usePremiumArcgisBasemaps(): boolean {
  return (import.meta.env.VITE_USE_PREMIUM_ARCGIS_BASEMAPS ?? '').trim() === '1';
}
