// frontend/src/components/ArcgisMap.tsx
// ArcGIS Maps SDK for JavaScript wrapper (replaces the previous Leaflet map).
// Renders bridge inventory as a client-side FeatureLayer with either a heatmap
// or condition-coloured markers, plus a rich popup per bridge.
import React, { useEffect, useRef, useState } from 'react';
import { Loader2, AlertTriangle } from 'lucide-react';
import { loadEsriModules, getArcgisApiKey, usePremiumArcgisBasemaps } from '../lib/arcgis';

// Rejects if `p` doesn't settle within `ms` — prevents a referrer-locked API key
// (whose basemap never authenticates) from leaving the map spinning forever.
function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`${label} timed out after ${Math.round(ms / 1000)}s`)), ms);
    p.then(
      (v) => { clearTimeout(t); resolve(v); },
      (e) => { clearTimeout(t); reject(e); }
    );
  });
}


export interface MapBridge {
  bridge_name?: string;
  /** Server-derived "carried over below" name for map cards only (not an SNBI field). */
  display_name?: string | null;
  bridge_location?: string;
  bridge_number: string;
  latitude: number;
  longitude: number;
  state_code?: string | number;
  state_name?: string;
  county_code?: string | number;
  data_year?: string | number;
  year_built?: string | number;
  bridge_condition_classification?: string;
  lowest_condition_rating_code?: string | number;
  deck_condition_rating?: string | number;
  superstructure_condition_rating?: string | number;
  substructure_condition_rating?: string | number;
  culvert_condition_rating?: string | number;
  channel_condition_rating?: string | number;
  scour_condition_rating?: string | number;
  bridge_railings_condition_rating?: string | number;
  bridge_bearings_condition_rating?: string | number;
  bridge_joints_condition_rating?: string | number;
  total_bridge_length?: string | number;
  bridge_width_out_to_out?: string | number;
}

export type MapMode = 'heatmap' | 'markers';
export type BaseLayer = 'streets' | 'satellite';
export type BridgeAction = 'open' | 'history' | 'reports' | 'edit' | 'lcca' | 'explorer';

export interface PopupBridgeRef {
  state_code: string | number;
  bridge_number: string;
  bridge_name?: string;
  // Extra fields carried for the LCCA action (avoids a second fetch).
  year_built?: string | number;
  total_bridge_length?: string | number;
  bridge_width_out_to_out?: string | number;
  deck_condition_rating?: string | number;
  superstructure_condition_rating?: string | number;
  substructure_condition_rating?: string | number;
  lowest_condition_rating_code?: string | number;
}

interface ArcgisMapProps {
  bridges: MapBridge[];
  mode: MapMode;
  /** Condition field used for heatmap weight / marker colour. */
  feature: string;
  basemap: BaseLayer;
  center?: [number, number];
  zoom?: number;
  className?: string;
  /** Hide zoom controls (used for the landing-page hero). */
  minimalUi?: boolean;
  /** When set, the bridge popup shows action buttons (History / Reports / Edit). */
  onBridgeAction?: (action: BridgeAction, bridge: PopupBridgeRef) => void;
  /** Draw NJ county boundary outlines + labels. */
  showCounties?: boolean;
  /** Called with the 3-digit county code when a county polygon is clicked. */
  onCountySelect?: (countyCode: string) => void;
}

const toNum = (v: unknown): number | null => {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isNaN(n) ? null : n;
};

// Higher weight = worse condition (drives heatmap intensity).
const severity = (v: unknown): number => {
  const n = toNum(v);
  if (n === null) return 0.3;
  if (n >= 7) return 0.3; // Good
  if (n >= 5) return 0.65; // Fair
  return 1.0; // Poor / critical
};

const ratingColor = (v: unknown): string => {
  const n = toNum(v);
  if (n === null) return '#94a3b8';
  if (n >= 7) return '#16a34a';
  if (n >= 5) return '#ca8a04';
  return '#dc2626';
};

const ratingLabel = (v: unknown): string => {
  const n = toNum(v);
  if (n === null) return 'N/A';
  if (n >= 7) return 'Good';
  if (n >= 5) return 'Fair';
  return 'Poor';
};

const POPUP_ROWS: { label: string; key: keyof MapBridge }[] = [
  { label: 'Deck', key: 'deck_condition_rating' },
  { label: 'Superstructure', key: 'superstructure_condition_rating' },
  { label: 'Substructure', key: 'substructure_condition_rating' },
  { label: 'Culvert', key: 'culvert_condition_rating' },
  { label: 'Channel', key: 'channel_condition_rating' },
  { label: 'Scour', key: 'scour_condition_rating' },
  { label: 'Railings', key: 'bridge_railings_condition_rating' },
  { label: 'Bearings', key: 'bridge_bearings_condition_rating' },
  { label: 'Joints', key: 'bridge_joints_condition_rating' },
];

// Fields the popup needs, carried as feature attributes.
const ATTR_KEYS: (keyof MapBridge)[] = [
  'bridge_name',
  'bridge_location',
  'bridge_number',
  'state_code',
  'county_code',
  'state_name',
  'year_built',
  'bridge_condition_classification',
  'lowest_condition_rating_code',
  ...POPUP_ROWS.map((r) => r.key),
  'total_bridge_length',
  'bridge_width_out_to_out',
];

const STRING_ATTR_KEYS = new Set<keyof MapBridge>([
  'bridge_name',
  'bridge_location',
  'bridge_number',
  'state_code',
  'county_code',
  'state_name',
  'year_built',
  'bridge_condition_classification',
]);

const isValidCoordinate = (bridge: MapBridge): boolean => {
  const lat = toNum(bridge.latitude);
  const lon = toNum(bridge.longitude);
  return (
    lat !== null &&
    lon !== null &&
    lat >= -90 &&
    lat <= 90 &&
    lon >= -180 &&
    lon <= 180
  );
};

const fieldValue = (bridge: MapBridge, key: keyof MapBridge): string | number | null => {
  const value = bridge[key];
  if (value === null || value === undefined || value === '') return null;
  if (STRING_ATTR_KEYS.has(key)) return String(value);
  return toNum(value);
};

function resolveBasemap(basemap: BaseLayer, usePremium: boolean): string {
  if (basemap === 'satellite') return usePremium ? 'arcgis/imagery' : 'satellite';
  return usePremium ? 'arcgis/navigation' : 'osm';
}

// Builds the popup DOM for a clicked bridge.
function buildPopupContent(
  attrs: Record<string, unknown>,
  onAction?: (action: BridgeAction, bridge: PopupBridgeRef) => void
): HTMLElement {
  const wrap = document.createElement('div');
  wrap.style.cssText =
    'font-family:system-ui,sans-serif;font-size:13px;line-height:1.45;min-width:250px';

  const lowest = attrs['lowest_condition_rating_code'];
  const klass = (attrs['bridge_condition_classification'] as string) || ratingLabel(lowest);
  const kcolor = ratingColor(lowest);

  // Identity: structure number + NBI location. When the title is a real name
  // (SNBI B.ID.02 or the derived feature name), keep the structure number visible.
  const header = document.createElement('div');
  const titleIsName =
    attrs['display_name'] && attrs['display_name'] !== `Bridge ${attrs['bridge_number'] ?? ''}`;
  const sub = [
    titleIsName ? `Bridge ${attrs['bridge_number'] ?? ''}` : null,
    (attrs['bridge_location'] as string) || null,
  ].filter(Boolean).join(' · ');
  const meta = [attrs['state_name'], attrs['year_built'] ? `Built ${attrs['year_built']}` : null]
    .filter(Boolean)
    .join('  ·  ');
  header.innerHTML =
    (sub ? `<div style="color:#64748b;font-size:11.5px;margin:-2px 0 8px;overflow:hidden;` +
           `text-overflow:ellipsis;white-space:nowrap" title="${sub}">${sub}</div>` : '') +
    `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:9px">` +
    `<span style="display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:9999px;` +
    `font-weight:600;font-size:11.5px;background:${kcolor}14;color:${kcolor};border:1px solid ${kcolor}33">` +
    `<span style="width:6px;height:6px;border-radius:50%;background:${kcolor}"></span>${klass}</span>` +
    `<span style="color:#94a3b8;font-size:11px">Lowest: <b style="color:#475569">${lowest ?? 'N/A'}</b></span></div>` +
    (meta ? `<div style="color:#64748b;font-size:11.5px;margin-bottom:9px">${meta}</div>` : '');
  wrap.appendChild(header);

  // Compact primary-condition chips (the rest live in Bridge Details).
  const PRIMARY: Array<[string, string]> = [
    ['Deck', 'deck_condition_rating'],
    ['Super', 'superstructure_condition_rating'],
    ['Sub', 'substructure_condition_rating'],
    ['Culvert', 'culvert_condition_rating'],
  ];
  const grid = document.createElement('div');
  grid.style.cssText = 'display:flex;gap:6px;margin-bottom:10px';
  for (const [label, key] of PRIMARY) {
    const val = attrs[key];
    if (val === null || val === undefined || val === '') continue;
    const c = ratingColor(val);
    const chip = document.createElement('div');
    chip.style.cssText =
      `flex:1;text-align:center;border-radius:9px;padding:6px 4px;background:${c}0f;border:1px solid ${c}2e`;
    chip.innerHTML =
      `<div style="font-size:9.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:#64748b">${label}</div>` +
      `<div style="font-size:17px;font-weight:750;color:${c};line-height:1.2">${val}</div>`;
    grid.appendChild(chip);
  }
  if (grid.childElementCount) wrap.appendChild(grid);

  const len = attrs['total_bridge_length'];
  const wid = attrs['bridge_width_out_to_out'];
  if (len || wid) {
    const dims = document.createElement('div');
    dims.style.cssText =
      'border-top:1px solid #eef1f5;padding-top:7px;font-size:12px;display:flex;gap:18px';
    dims.innerHTML =
      (len ? `<span style="color:#94a3b8">Length <b style="color:#334155;font-weight:600">${len} ft</b></span>` : '') +
      (wid ? `<span style="color:#94a3b8">Width <b style="color:#334155;font-weight:600">${wid} ft</b></span>` : '');
    wrap.appendChild(dims);
  }

  if (onAction) {
    const ref: PopupBridgeRef = {
      state_code: (attrs['state_code'] as string | number) ?? '',
      bridge_number: String(attrs['bridge_number'] ?? ''),
      bridge_name: (attrs['bridge_name'] as string) || undefined,
      year_built: attrs['year_built'] as string | number | undefined,
      total_bridge_length: attrs['total_bridge_length'] as string | number | undefined,
      bridge_width_out_to_out: attrs['bridge_width_out_to_out'] as string | number | undefined,
      deck_condition_rating: attrs['deck_condition_rating'] as string | number | undefined,
      superstructure_condition_rating: attrs['superstructure_condition_rating'] as string | number | undefined,
      substructure_condition_rating: attrs['substructure_condition_rating'] as string | number | undefined,
      lowest_condition_rating_code: attrs['lowest_condition_rating_code'] as string | number | undefined,
    };
    const bar = document.createElement('div');
    bar.style.cssText =
      'display:flex;flex-direction:column;gap:6px;border-top:1px solid #eef1f5;padding-top:9px;margin-top:9px';

    const mkBtn = (label: string, action: BridgeAction, primary: boolean) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.style.cssText = primary
        ? 'flex:1;cursor:pointer;border:1px solid transparent;border-radius:9px;padding:7px 10px;' +
          'font-size:12.5px;font-weight:600;color:#fff;background:#4f46e5;' +
          'box-shadow:0 1px 2px rgba(79,70,229,.35);transition:background .15s'
        : 'flex:1;min-width:0;cursor:pointer;border:1px solid #dde2ea;border-radius:9px;padding:6px 8px;' +
          'font-size:11.5px;font-weight:600;color:#475569;background:#fff;transition:background .15s,border-color .15s';
      b.addEventListener('mouseenter', () => {
        if (primary) b.style.background = '#4338ca';
        else { b.style.background = '#f4f6fb'; b.style.borderColor = '#c7cdd9'; }
      });
      b.addEventListener('mouseleave', () => {
        b.style.background = primary ? '#4f46e5' : '#fff';
        if (!primary) b.style.borderColor = '#dde2ea';
      });
      b.addEventListener('click', () => onAction(action, ref));
      return b;
    };

    bar.appendChild(mkBtn('Open Bridge Details →', 'open', true));
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:6px';
    row.appendChild(mkBtn('History', 'history', false));
    row.appendChild(mkBtn('LCCA', 'lcca', false));
    bar.appendChild(row);
    wrap.appendChild(bar);
  }

  return wrap;
}

const ArcgisMap: React.FC<ArcgisMapProps> = ({
  bridges,
  mode,
  feature,
  basemap,
  center = [-98.5795, 39.8283],
  zoom = 4,
  className = '',
  minimalUi = false,
  onBridgeAction,
  showCounties = false,
  onCountySelect,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<any>(null);
  const mapRef = useRef<any>(null);
  const layerRef = useRef<any>(null);
  const countyLayerRef = useRef<any>(null);
  const esriRef = useRef<Record<string, any>>({});
  // Always call the latest handlers without rebuilding the layer.
  const onActionRef = useRef(onBridgeAction);
  onActionRef.current = onBridgeAction;
  const onCountyRef = useRef(onCountySelect);
  onCountyRef.current = onCountySelect;

  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // True once we've had to fall back to a keyless basemap (premium key/referrer
  // failed). Subsequent basemap swaps then stay keyless to avoid re-stalling.
  const keylessRef = useRef(false);

  // One-time SDK load + MapView creation.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const [
          esriConfig,
          EsriMap,
          MapView,
          FeatureLayer,
          Graphic,
          HeatmapRenderer,
          ClassBreaksRenderer,
          SimpleMarkerSymbol,
        ] = await withTimeout(
          loadEsriModules([
            'esri/config',
            'esri/Map',
            'esri/views/MapView',
            'esri/layers/FeatureLayer',
            'esri/Graphic',
            'esri/renderers/HeatmapRenderer',
            'esri/renderers/ClassBreaksRenderer',
            'esri/symbols/SimpleMarkerSymbol',
          ]),
          35000,
          'ArcGIS SDK load'
        );

        if (cancelled || !containerRef.current) return;

        const usePremium = Boolean(getArcgisApiKey()) && usePremiumArcgisBasemaps();
        if (usePremium) esriConfig.apiKey = getArcgisApiKey();

        esriRef.current = { FeatureLayer, Graphic, HeatmapRenderer, ClassBreaksRenderer, SimpleMarkerSymbol };

        // Create a MapView for a given basemap, resolving on view.when() or
        // rejecting on timeout (a referrer-locked premium key stalls forever).
        const buildView = async (bm: string, waitMs: number) => {
          const map = new EsriMap({ basemap: bm });
          const view = new MapView({
            container: containerRef.current!,
            map,
            center,
            zoom,
            ui: { components: minimalUi ? ['attribution'] : ['zoom', 'attribution'] },
            popup: {
              // Compact, single-bridge card that never docks to a corner and
              // never shows the multi-feature pager.
              dockEnabled: false,
              collapseEnabled: false,
              dockOptions: { buttonEnabled: false, breakpoint: false },
              visibleElements: { featureNavigation: false, collapseButton: false },
            },
          });
          try {
            await withTimeout(view.when(), waitMs, `basemap "${bm}"`);
            return { map, view };
          } catch (e) {
            try { view.destroy(); } catch { /* ignore */ }
            throw e;
          }
        };

        const keyless = basemap === 'satellite' ? 'satellite' : 'osm';
        // Use public keyless basemaps by default. Premium ArcGIS basemaps are
        // opt-in because an invalid/referrer-blocked key opens the native
        // ArcGIS Online sign-in dialog over our app.
        const primary = resolveBasemap(basemap, usePremium);

        let built: { map: any; view: any };
        try {
          built = await buildView(primary, 35000);
        } catch (e1) {
          if (cancelled) return;
          if (primary === keyless) throw e1; // already keyless — genuine failure
          console.warn(`[ArcgisMap] "${primary}" basemap failed. Falling back to "${keyless}".`, e1);
          keylessRef.current = true; // this view stays keyless; other maps still try premium
          built = await buildView(keyless, 35000);
        }

        if (cancelled) { try { built.view.destroy(); } catch { /* ignore */ } return; }
        mapRef.current = built.map;
        viewRef.current = built.view;

        // NJ county boundary outlines + labels (kept beneath the bridge layer).
        // This hosted county service now requires an ArcGIS token. Load it only
        // when premium ArcGIS access was explicitly configured; keyless mode
        // must never open the SDK's credential dialog.
        if (showCounties && usePremium) {
          try {
            const countyLayer = new FeatureLayer({
              url: 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Counties_Generalized/FeatureServer/0',
              definitionExpression: "STATE_FIPS = '34'",
              outFields: ['NAME', 'FIPS', 'COUNTY_FIPS'],
              renderer: {
                type: 'simple',
                symbol: {
                  type: 'simple-fill',
                  color: [99, 102, 241, 0.04],
                  outline: { color: [99, 102, 241, 0.9], width: 1.4 },
                },
              },
              labelingInfo: [{
                labelExpressionInfo: { expression: '$feature.NAME' },
                symbol: { type: 'text', color: [148, 163, 184, 1], haloColor: [2, 6, 23, 0.7], haloSize: 1, font: { size: 10, weight: 'bold' } },
                labelPlacement: 'always-horizontal',
              }],
              popupEnabled: false,
              legendEnabled: false,
              title: 'NJ Counties',
            });
            built.map.add(countyLayer, 0);
            countyLayerRef.current = countyLayer;
          } catch (e) { console.warn('[ArcgisMap] county layer failed', e); }
        }

        // Click a county polygon → select it (when no bridge marker was hit).
        if (onCountyRef.current) {
          built.view.on('click', async (event: any) => {
            try {
              const res = await built.view.hitTest(event);
              const hits = res.results || [];
              const onBridge = hits.some((h: any) => h.graphic?.layer === layerRef.current);
              if (onBridge) return; // bridge popup handles it
              const countyHit = hits.find((h: any) => h.graphic?.layer === countyLayerRef.current);
              const attrs = countyHit?.graphic?.attributes;
              const fips = attrs?.FIPS ?? attrs?.COUNTY_FIPS;
              if (fips != null) {
                const code = String(fips).slice(-3); // last 3 digits = county code
                onCountyRef.current?.(String(parseInt(code, 10)));
              }
            } catch { /* ignore */ }
          });
        }

        setReady(true);
      } catch (err: any) {
        if (!cancelled) setError(err?.message || 'Failed to initialise the map.');
      }
    })();

    return () => {
      cancelled = true;
      if (viewRef.current) {
        viewRef.current.destroy();
        viewRef.current = null;
      }
    };
    // Intentionally one-time: subsequent prop changes are handled by the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Rebuild the data layer when bridges / mode / feature change.
  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const { FeatureLayer, Graphic, HeatmapRenderer, ClassBreaksRenderer, SimpleMarkerSymbol } =
      esriRef.current;

    if (layerRef.current) {
      mapRef.current.remove(layerRef.current);
      layerRef.current = null;
    }
    if (!bridges.length) return;

    const graphics = bridges.filter(isValidCoordinate).map((b, i) => {
      const latitude = toNum(b.latitude) as number;
      const longitude = toNum(b.longitude) as number;
      const attrs: Record<string, unknown> = {
        ObjectID: i + 1,
        weight: severity((b as any)[feature]),
        condValue: toNum((b as any)[feature]) ?? -1,
      };
      for (const k of ATTR_KEYS) attrs[k] = fieldValue(b, k);
      // Card title: SNBI B.ID.02 name when present, else the server-derived
      // "carried over below" feature name, else the structure number.
      attrs.display_name =
        (fieldValue(b, 'bridge_name') as string | null) ||
        (typeof b.display_name === 'string' && b.display_name.trim() ? b.display_name.trim() : null) ||
        `Bridge ${b.bridge_number}`;
      return new Graphic({
        geometry: { type: 'point', longitude, latitude },
        attributes: attrs,
      });
    });

    const marker = (color: string) =>
      new SimpleMarkerSymbol({
        style: 'circle',
        color,
        size: 7,
        outline: { color: '#ffffff', width: 0.6 },
      });

    const renderer =
      mode === 'heatmap'
        ? new HeatmapRenderer({
            field: 'weight',
            radius: 18,
            colorStops: [
              { ratio: 0, color: 'rgba(0,0,0,0)' },
              { ratio: 0.2, color: 'rgba(96,165,250,0.55)' },
              { ratio: 0.45, color: 'rgba(251,191,36,0.75)' },
              { ratio: 0.7, color: 'rgba(248,113,113,0.85)' },
              { ratio: 1, color: 'rgba(153,27,27,0.95)' },
            ],
          })
        : new ClassBreaksRenderer({
            field: 'condValue',
            defaultSymbol: marker('#94a3b8'),
            defaultLabel: 'No data',
            classBreakInfos: [
              { minValue: 0, maxValue: 4.999, symbol: marker('#dc2626'), label: 'Poor (0–4)' },
              { minValue: 5, maxValue: 6.999, symbol: marker('#ca8a04'), label: 'Fair (5–6)' },
              { minValue: 7, maxValue: 9, symbol: marker('#16a34a'), label: 'Good (7–9)' },
            ],
          });

    const fields = [
      { name: 'ObjectID', type: 'oid' },
      { name: 'weight', type: 'double' },
      { name: 'condValue', type: 'double' },
      { name: 'display_name', type: 'string' },
      ...ATTR_KEYS.map((k) => ({
        name: k as string,
        type: STRING_ATTR_KEYS.has(k) ? 'string' : 'double',
      })),
    ];

    const layer = new FeatureLayer({
      source: graphics,
      objectIdField: 'ObjectID',
      geometryType: 'point',
      spatialReference: { wkid: 4326 },
      fields,
      renderer,
      popupEnabled: true,
      popupTemplate: {
        title: '{display_name}',
        outFields: ['*'],
        content: (f: any) => buildPopupContent(f.graphic.attributes, onActionRef.current),
      },
    });

    layerRef.current = layer;
    mapRef.current.add(layer);
  }, [ready, bridges, mode, feature]);

  // Swap basemap without rebuilding the view. If we already fell back to keyless
  // (premium key/referrer failed), keep using keyless basemaps so it can't re-stall.
  useEffect(() => {
    if (!ready || !mapRef.current) return;
    const usePremium = Boolean(getArcgisApiKey()) && usePremiumArcgisBasemaps() && !keylessRef.current;
    mapRef.current.basemap = resolveBasemap(basemap, usePremium);
  }, [ready, basemap]);

  return (
    <div className={`relative ${className}`}>
      <div ref={containerRef} className="absolute inset-0" />

      {!ready && !error && (
        <div className="absolute inset-0 flex items-center justify-center bg-gray-100/70 dark:bg-gray-900/70 backdrop-blur-sm z-[500]">
          <Loader2 className="w-10 h-10 animate-spin text-blue-600" />
        </div>
      )}

      {error && (
        <div className="absolute inset-0 flex items-center justify-center p-8 text-center bg-gray-100 dark:bg-gray-900 z-[500]">
          <div>
            <AlertTriangle className="w-10 h-10 text-amber-500 mx-auto mb-3" />
            <p className="font-semibold text-gray-800 dark:text-gray-100 mb-1">Map failed to load</p>
            <p className="text-sm text-gray-500 dark:text-gray-400">{error}</p>
          </div>
        </div>
      )}
    </div>
  );
};

export default ArcgisMap;
