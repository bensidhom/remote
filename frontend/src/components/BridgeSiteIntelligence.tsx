import React, { useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import {
  AlertTriangle, Camera, CloudSun, ExternalLink, Eye, Layers3, Loader2,
  MapPin, Mountain, Navigation, RefreshCw, ShieldCheck, Waves,
} from 'lucide-react';
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { getArcgisApiKey, loadEsriModules } from '../lib/arcgis';

type SectionState = 'available' | 'not_configured' | 'not_applicable' | 'unavailable' | 'rate_limited' | 'missing';
type SiteSection = {
  status: SectionState;
  provider?: string;
  source_url?: string;
  message?: string;
  cache_state?: 'fresh' | 'stale' | 'missing';
  stale?: boolean;
  fetched_at?: string;
  refresh_error?: string;
  [key: string]: any;
};
type SiteResponse = {
  bridge: {
    state_code: number | string;
    bridge_number: string;
    data_year: number;
    bridge_name?: string;
    bridge_location?: string;
    latitude: number;
    longitude: number;
    water_crossing?: boolean;
    waterway_names?: string[];
    [key: string]: any;
  };
  sections: Record<string, SiteSection>;
  generated_at: string;
};
type LayerResponse = {
  status: 'available';
  layer: string;
  label: string;
  category: string;
  source_url: string;
  search_radius_miles?: number | null;
  geometry_type?: string;
  fields: Array<{ name: string; alias?: string; type?: string }>;
  features: Array<{ attributes?: Record<string, any>; geometry?: Record<string, any> }>;
  summaries: Array<{ field: string; label: string; values: Array<{ label: string; count: number }> }>;
  exceeded_transfer_limit?: boolean;
  fetched_at?: string;
  cache_state?: 'fresh' | 'stale';
};

const SECTION_NAMES = ['weather', 'traffic', 'water', 'flood', 'terrain', 'constraints', 'imagery'] as const;
const MAP_LAYER_GROUPS = [
  { key: 'live', label: 'Live & observations', layers: ['traffic_count_sites', 'water_gages', 'live_traffic'] },
  { key: 'flood', label: 'Flood hazards', layers: ['fema_flood_zones', 'base_flood_elevations'] },
  { key: 'property', label: 'Property & land cover', layers: ['parcels', 'land_use_2015', 'impervious_surface_2012'] },
  { key: 'environmental', label: 'Environmental', layers: ['national_wetlands', 'njdep_wetlands_2012', 'epa_priority_wetlands', 'contaminated_sites', 'brownfield_inventory', 'federally_regulated_sites', 'underground_storage_tanks', 'immediate_environmental_concerns', 'deed_notice_areas'] },
  { key: 'geotechnical', label: 'Ground & earthwork', layers: ['soils', 'landslides', 'historic_fill', 'potential_acid_soils', 'abandoned_mines', 'bedrock_geology', 'groundwater_recharge', 'sole_source_aquifer', 'surficial_geology'] },
  { key: 'water', label: 'Water & resilience', layers: ['watershed', 'category_one_waters', 'surface_water_classification', 'tmdl_streamshed', 'wellhead_protection', 'head_of_tide', 'tidal_climate_adjusted_flood', 'nearby_dams'] },
  { key: 'historic_ecology', label: 'Historic & ecological', layers: ['historic_properties', 'historic_districts', 'archaeological_site_grid', 'natural_heritage_priority'] },
  { key: 'coordination', label: 'Utilities & coordination', layers: ['building_footprints', 'mapped_utility_lines', 'sewer_service_area', 'electric_utility_territory', 'gas_utility_territory'] },
] as const;

const statusLabel = (section?: SiteSection) => {
  if (!section || section.status === 'missing') return 'Waiting';
  if (section.stale || section.cache_state === 'stale') return 'Cached';
  return ({
    available: 'Current', not_configured: 'Setup needed', not_applicable: 'Not applicable',
    unavailable: 'Unavailable', rate_limited: 'Rate limited', missing: 'Waiting',
  } as Record<string, string>)[section.status] || section.status;
};

const statusClass = (section?: SiteSection) => {
  if (section?.status === 'available' && !section.stale) return 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-800';
  if (section?.status === 'not_applicable') return 'bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700';
  return 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800';
};

const pretty = (value: unknown): string => {
  if (value === null || value === undefined || value === '') return 'Not reported';
  if (typeof value === 'number') return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return String(value).replace(/_/g, ' ');
};

const orderedFields = (fields: LayerResponse['fields'], features: LayerResponse['features']) => {
  const meaningful = /(status|cell|label|name|type|class|category|zone|use|county|municip)/i;
  const audit = /(^objectid$|^fid$|globalid|shape|created_|last_edited|editor|creator)/i;
  return fields
    .filter((field) => field.name && !audit.test(field.name) && features.some((feature) => feature.attributes?.[field.name] != null))
    .sort((left, right) => Number(!meaningful.test(left.name + left.alias)) - Number(!meaningful.test(right.name + right.alias)));
};

const displayAttribute = (field: LayerResponse['fields'][number], value: unknown) => {
  if (value != null && /date/i.test(field.type || '') && Number.isFinite(Number(value))) {
    return dateTime(new Date(Number(value)).toISOString());
  }
  return pretty(value);
};

const OVERLAY_COLORS = [
  [79, 70, 229], [14, 165, 233], [5, 150, 105], [245, 158, 11],
  [225, 29, 72], [124, 58, 237], [8, 145, 178], [101, 116, 205],
];

const overlayColor = (name: string) => {
  let hash = 0;
  for (const character of name) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
  return OVERLAY_COLORS[Math.abs(hash) % OVERLAY_COLORS.length];
};

const dateTime = (value?: string) => {
  if (!value) return 'Not reported';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
};

type TerrainMetric = 'elevation_feet' | 'slope_percent';

const TerrainGrid: React.FC<{
  grid: any;
  metric: TerrainMetric;
  onMetricChange: (metric: TerrainMetric) => void;
}> = ({ grid, metric, onMetricChange }) => {
  const samples = grid?.samples || [];
  const values = samples.map((item: any) => Number(item?.[metric])).filter(Number.isFinite);
  if (!samples.length || !values.length) return null;
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const range = maximum - minimum || 1;
  const unit = metric === 'elevation_feet' ? 'ft' : '%';
  const label = metric === 'elevation_feet' ? 'Elevation' : 'Surface slope';
  const cellColor = (value: unknown) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '#e2e8f0';
    const normalized = Math.max(0, Math.min(1, (numeric - minimum) / range));
    return metric === 'elevation_feet'
      ? `hsl(${210 - normalized * 175} 72% ${78 - normalized * 28}%)`
      : `hsl(${165 - normalized * 150} 75% ${80 - normalized * 28}%)`;
  };
  const legendGradient = metric === 'elevation_feet'
    ? 'linear-gradient(90deg, hsl(210 72% 78%), hsl(123 72% 64%), hsl(35 72% 50%))'
    : 'linear-gradient(90deg, hsl(165 75% 80%), hsl(90 75% 66%), hsl(15 75% 52%))';

  return <div className="mx-auto w-full max-w-[360px] rounded-2xl border border-slate-200 bg-slate-50/70 p-3 dark:border-slate-700 dark:bg-slate-800/40 lg:mx-0">
    <div className="flex items-center justify-between gap-3">
      <div>
        <p className="text-xs font-semibold text-slate-700 dark:text-slate-200">Bridge-centered terrain grid</p>
        <p className="text-[10px] text-slate-400">{grid.size}×{grid.size} cells · {pretty(grid.spacing_feet)} ft spacing</p>
      </div>
      <div className="inline-flex rounded-lg bg-white p-0.5 shadow-sm ring-1 ring-slate-200 dark:bg-slate-900 dark:ring-slate-700">
        {([['elevation_feet', 'Elevation'], ['slope_percent', 'Slope']] as const).map(([value, text]) => <button
          key={value}
          type="button"
          onClick={() => onMetricChange(value)}
          className={`rounded-md px-2 py-1 text-[10px] font-semibold transition ${metric === value ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/60 dark:text-indigo-200' : 'text-slate-500 hover:text-slate-700 dark:text-slate-400'}`}
        >{text}</button>)}
      </div>
    </div>
    <div className="mt-3 grid grid-cols-[16px_minmax(0,1fr)_16px] grid-rows-[16px_minmax(0,1fr)_16px] items-center gap-1">
      <span className="col-start-2 row-start-1 text-center text-[10px] font-bold text-slate-400">N</span>
      <span className="col-start-1 row-start-2 text-center text-[10px] font-bold text-slate-400">W</span>
      <div
        className="col-start-2 row-start-2 grid aspect-square overflow-hidden rounded-lg bg-slate-200 shadow-inner"
        style={{ gridTemplateColumns: `repeat(${grid.size}, minmax(0, 1fr))`, gap: '1px' }}
      >
        {samples.map((sample: any, index: number) => {
          const value = sample?.[metric];
          const center = sample.row === Math.floor(grid.size / 2) && sample.column === Math.floor(grid.size / 2);
          return <div
            key={`${sample.row}-${sample.column}-${index}`}
            className={`relative min-w-0 ${center ? 'z-10 ring-2 ring-white ring-offset-1 ring-offset-indigo-600' : ''}`}
            style={{ backgroundColor: cellColor(value) }}
            title={`${center ? 'Bridge center · ' : ''}${label}: ${Number.isFinite(Number(value)) ? `${Number(value).toFixed(metric === 'elevation_feet' ? 1 : 2)} ${unit}` : 'not available'} · ${Math.abs(sample.north_offset_feet)} ft ${sample.north_offset_feet >= 0 ? 'north' : 'south'}, ${Math.abs(sample.east_offset_feet)} ft ${sample.east_offset_feet >= 0 ? 'east' : 'west'}`}
          >{center && <span className="absolute inset-0 grid place-items-center text-[8px] font-black text-white drop-shadow">●</span>}</div>;
        })}
      </div>
      <span className="col-start-3 row-start-2 text-center text-[10px] font-bold text-slate-400">E</span>
      <span className="col-start-2 row-start-3 text-center text-[10px] font-bold text-slate-400">S</span>
    </div>
    <div className="mt-2 flex items-center gap-2 text-[10px] text-slate-500">
      <span>{minimum.toFixed(metric === 'elevation_feet' ? 1 : 2)} {unit}</span>
      <span className="h-2 flex-1 rounded-full" style={{ background: legendGradient }} />
      <span>{maximum.toFixed(metric === 'elevation_feet' ? 1 : 2)} {unit}</span>
    </div>
    <p className="mt-1 text-center text-[10px] text-slate-400">White marker = bridge inventory coordinate</p>
  </div>;
};

const SectionCard: React.FC<{
  title: string;
  icon: React.ReactNode;
  section?: SiteSection;
  children: React.ReactNode;
  className?: string;
}> = ({ title, icon, section, children, className = '' }) => (
  <section className={`rounded-2xl border border-slate-200/80 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-sm ${className}`}>
    <div className="flex items-start justify-between gap-3 px-4 py-3 border-b border-slate-100 dark:border-slate-800">
      <div className="flex items-center gap-2.5 min-w-0">
        <span className="grid place-items-center w-8 h-8 rounded-xl bg-brand-50 text-brand-600 dark:bg-brand-950/50 dark:text-brand-300">{icon}</span>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">{title}</h3>
          <p className="text-[11px] text-slate-400 truncate">{section?.provider || 'BridgeGuard site screening'}</p>
        </div>
      </div>
      <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${statusClass(section)}`}>{statusLabel(section)}</span>
    </div>
    <div className="p-4">{children}</div>
    {(section?.source_url || section?.fetched_at) && (
      <div className="flex items-center justify-between gap-3 px-4 py-2.5 border-t border-slate-100 dark:border-slate-800 text-[11px] text-slate-400">
        <span>{section?.fetched_at ? `Updated ${dateTime(section.fetched_at)}` : 'External reference'}</span>
        {section?.source_url && <a href={section.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-brand-600 dark:text-brand-300 hover:underline">Source <ExternalLink className="w-3 h-3" /></a>}
      </div>
    )}
  </section>
);

const EmptyState: React.FC<{ section?: SiteSection }> = ({ section }) => (
  <div className="rounded-xl bg-slate-50 dark:bg-slate-800/60 px-3 py-3 text-sm text-slate-500 dark:text-slate-400">
    {section?.refresh_error || section?.message || 'This source has not returned bridge-specific information yet.'}
  </div>
);

const Fact: React.FC<{ label: string; value: unknown; accent?: boolean }> = ({ label, value, accent }) => (
  <div className="rounded-xl bg-slate-50 dark:bg-slate-800/60 px-3 py-2.5">
    <p className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</p>
    <p className={`mt-0.5 text-sm font-semibold ${accent ? 'text-brand-600 dark:text-brand-300' : 'text-slate-700 dark:text-slate-200'}`}>{pretty(value)}</p>
  </div>
);

const LayerToggle: React.FC<{
  label: string;
  active: boolean;
  loading?: boolean;
  disabled?: boolean;
  count?: number | null;
  error?: string;
  onClick: () => void;
}> = ({ label, active, loading, disabled, count, error, onClick }) => (
  <button type="button" onClick={onClick} disabled={disabled || loading} title={error || `${active ? 'Hide' : 'Show'} ${label}`}
    className={`w-full flex items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-xs transition ${active ? 'border-indigo-300 bg-indigo-50 text-indigo-800 dark:border-indigo-700 dark:bg-indigo-950/50 dark:text-indigo-200' : 'border-transparent text-slate-600 hover:border-slate-200 hover:bg-white dark:text-slate-300 dark:hover:border-slate-700 dark:hover:bg-slate-900'} disabled:cursor-not-allowed disabled:opacity-45`}>
    {loading ? <Loader2 className="w-3.5 h-3.5 shrink-0 animate-spin" /> : active ? <Eye className="w-3.5 h-3.5 shrink-0" /> : <Layers3 className="w-3.5 h-3.5 shrink-0 text-slate-400" />}
    <span className="min-w-0 flex-1 truncate font-medium">{label}</span>
    {error ? <AlertTriangle className="w-3.5 h-3.5 shrink-0 text-red-500" /> : count != null && <span className="rounded-full bg-white px-1.5 py-0.5 text-[10px] font-semibold text-slate-500 shadow-sm dark:bg-slate-800 dark:text-slate-300">{count}</span>}
  </button>
);

const SiteMap: React.FC<{
  site: SiteResponse;
  showTraffic: boolean;
  showTrafficCounts: boolean;
  showWaterGages: boolean;
  survey?: any;
  content: string;
  imagerySource: string;
  viewMode: '2d' | '3d';
  activeLayers: LayerResponse[];
}> = ({ site, showTraffic, showTrafficCounts, showWaterGages, survey, content, imagerySource, viewMode, activeLayers }) => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const visibleOverlayCount = activeLayers.length + Number(showTraffic) + Number(showTrafficCounts) + Number(showWaterGages);

  useEffect(() => {
    if (!containerRef.current) return;
    let cancelled = false;
    let view: any;
    const run = async () => {
      try {
        const [esriConfig, Map, View, Graphic, Point, SimpleMarkerSymbol, MapImageLayer, WebTileLayer, TileLayer, GraphicsLayer, geometryJsonUtils] = await loadEsriModules<any[]>([
          'esri/config', 'esri/Map', viewMode === '3d' ? 'esri/views/SceneView' : 'esri/views/MapView', 'esri/Graphic', 'esri/geometry/Point',
          'esri/symbols/SimpleMarkerSymbol', 'esri/layers/MapImageLayer', 'esri/layers/WebTileLayer', 'esri/layers/TileLayer',
          'esri/layers/GraphicsLayer', 'esri/geometry/support/jsonUtils',
        ]);
        if (cancelled || !containerRef.current) return;
        const key = getArcgisApiKey();
        if (key) esriConfig.apiKey = key;
        const layers: any[] = [];
        const surveyId = survey?.survey_id;
        const publicLayer = (site.sections.imagery?.public_imagery || []).find((item: any) => `nj-${item.year}` === imagerySource);
        if (imagerySource === 'nearmap' && surveyId) {
          layers.push(new WebTileLayer({
            urlTemplate: `/api/site-intelligence/nearmap-tiles/${encodeURIComponent(surveyId)}/${content}/{level}/{col}/{row}`,
            copyright: 'Nearmap', title: `Nearmap ${content}`,
          }));
        } else if (publicLayer?.map_service_url) {
          layers.push(new TileLayer({
            url: publicLayer.map_service_url,
            title: publicLayer.label,
          }));
        }
        if (showTraffic && key) {
          layers.push(new MapImageLayer({
            url: 'https://traffic.arcgis.com/arcgis/rest/services/World/Traffic/MapServer',
            title: 'Live traffic', opacity: 0.78,
          }));
        }
        const overlayLayers: any[] = [];
        for (const activeLayer of activeLayers) {
          if (!activeLayer?.features?.length) continue;
          const visibleFields = orderedFields(activeLayer.fields || [], activeLayer.features).slice(0, 12);
          const graphics = activeLayer.features.flatMap((feature) => {
            if (!feature.geometry) return [];
            try {
              const geometry = geometryJsonUtils.fromJSON({
                ...feature.geometry,
                spatialReference: feature.geometry.spatialReference || { wkid: 4326 },
              });
              const attributes = feature.attributes || {};
              const status = String(Object.entries(attributes).find(([name]) => name.toLowerCase() === 'status')?.[1] || '').toUpperCase().replace(/[\s-]+/g, '_');
              const statusColors: Record<string, number[]> = {
                NHL: [124, 58, 237], LISTED: [220, 38, 38], ELIGIBLE: [245, 158, 11],
                LOCALLY_DESIGNATED: [5, 150, 105], IDENTIFIED: [14, 165, 233],
                NOT_ELIGIBLE: [100, 116, 139], DELISTED: [71, 85, 105],
              };
              const color = statusColors[status] || overlayColor(activeLayer.layer);
              const symbol = geometry.type === 'polygon'
                ? { type: 'simple-fill', color: [...color, 0.22], outline: { color: [...color, 0.95], width: 2 } }
                : geometry.type === 'polyline'
                  ? { type: 'simple-line', color: [14, 165, 233, 0.95], width: 3 }
                  : { type: 'simple-marker', color: [245, 158, 11, 0.95], size: 9, outline: { color: [255, 255, 255, 1], width: 1.5 } };
              const mappedGraphic = new Graphic({
                geometry,
                attributes,
                symbol,
                popupTemplate: {
                  title: activeLayer.label,
                  content: [{
                    type: 'fields',
                    fieldInfos: visibleFields.map((field) => ({ fieldName: field.name, label: field.alias || pretty(field.name) })),
                  }],
                },
              });
              const labelValue = Object.entries(attributes).find(([name]) => /^(cell_num|label)$/i.test(name))?.[1];
              if (geometry.type === 'polygon' && labelValue && geometry.extent?.center) {
                return [mappedGraphic, new Graphic({
                  geometry: geometry.extent.center,
                  attributes,
                  symbol: { type: 'text', text: String(labelValue), color: '#312e81', haloColor: '#ffffff', haloSize: 1.5, font: { size: 10, weight: 'bold' } },
                })];
              }
              return [mappedGraphic];
            } catch { return []; }
          });
          const overlay = new GraphicsLayer({ title: activeLayer.label, graphics });
          overlayLayers.push(overlay);
          layers.push(overlay);
        }

        const observationGraphics: any[] = [];
        if (showTrafficCounts) {
          for (const count of site.sections.traffic?.traffic_counts || []) {
            if (!Number.isFinite(Number(count.longitude)) || !Number.isFinite(Number(count.latitude))) continue;
            observationGraphics.push(new Graphic({
              geometry: new Point({ longitude: Number(count.longitude), latitude: Number(count.latitude) }),
              attributes: count,
              symbol: { type: 'simple-marker', color: [249, 115, 22, 0.95], size: 8, outline: { color: '#ffffff', width: 1.5 } },
              popupTemplate: { title: 'NJDOT traffic count', content: [{ type: 'fields', fieldInfos: [{ fieldName: 'si_station_num', label: 'Station' }, { fieldName: 'aadt', label: 'AADT' }, { fieldName: 'count_year', label: 'Count year' }] }] },
            }));
          }
        }
        if (showWaterGages) {
          const gauges = [
            ...(site.sections.water?.nwps_gauges || []),
            ...(site.sections.water?.nearest_station ? [site.sections.water.nearest_station] : []),
            ...(site.sections.water?.coops_station ? [site.sections.water.coops_station] : []),
          ];
          for (const gauge of gauges) {
            if (!Number.isFinite(Number(gauge.longitude)) || !Number.isFinite(Number(gauge.latitude))) continue;
            observationGraphics.push(new Graphic({
              geometry: new Point({ longitude: Number(gauge.longitude), latitude: Number(gauge.latitude) }),
              attributes: gauge,
              symbol: { type: 'simple-marker', color: [2, 132, 199, 0.95], size: 9, outline: { color: '#ffffff', width: 1.5 } },
              popupTemplate: { title: 'Water gage', content: [{ type: 'fields', fieldInfos: [{ fieldName: 'station_name', label: 'Station' }, { fieldName: 'name', label: 'Name' }, { fieldName: 'distance_miles', label: 'Distance (mi)' }] }] },
            }));
          }
        }
        if (observationGraphics.length) {
          const observations = new GraphicsLayer({ title: 'Live observations', graphics: observationGraphics });
          overlayLayers.push(observations);
          layers.push(observations);
        }
        const basemap = imagerySource === 'osm'
          ? 'osm'
          : imagerySource === 'arcgis-topographic'
            ? (key ? 'arcgis/topographic' : 'topo-vector')
            : imagerySource === 'current' || (!publicLayer && !(imagerySource === 'nearmap' && surveyId))
              ? (key ? 'arcgis/imagery' : 'satellite')
              : undefined;
        const map = new Map({
          basemap,
          layers,
          ground: viewMode === '3d' ? 'world-elevation' : undefined,
        });
        view = new View({
          container: containerRef.current,
          map,
          center: [site.bridge.longitude, site.bridge.latitude],
          zoom: viewMode === '3d' ? 15 : 17,
          popup: { dockEnabled: false },
          ...(viewMode === '3d'
            ? { viewingMode: 'local', qualityProfile: 'medium', camera: { position: { longitude: site.bridge.longitude, latitude: site.bridge.latitude - 0.0045, z: 420 }, heading: 0, tilt: 66 } }
            : { constraints: { snapToZoom: false } }),
        });
        const marker = new Graphic({
          geometry: new Point({ longitude: site.bridge.longitude, latitude: site.bridge.latitude }),
          symbol: new SimpleMarkerSymbol({ color: '#4f46e5', size: 12, outline: { color: '#ffffff', width: 2 } }),
        });
        view.graphics.add(marker);
        await view.when();
        const targets = overlayLayers.flatMap((layer) => layer.graphics?.toArray?.() || []);
        if (targets.length && viewMode === '2d') {
          view.goTo(targets, { duration: 450 }).then(() => {
            if (view && view.zoom > 17) view.zoom = 17;
          }).catch(() => {});
        }
        if (cancelled) view.destroy();
      } catch (exc: any) {
        if (!cancelled) setError(exc?.message || 'The site map could not be loaded.');
      }
    };
    run();
    return () => { cancelled = true; if (view) view.destroy(); };
  }, [site, showTraffic, showTrafficCounts, showWaterGages, survey?.survey_id, content, imagerySource, viewMode, activeLayers]);

  return (
    <div className="relative h-[560px] rounded-2xl overflow-hidden bg-slate-100 dark:bg-slate-800 border border-slate-200 dark:border-slate-700">
      <div ref={containerRef} className="absolute inset-0" />
      {error && <div className="absolute inset-0 grid place-items-center p-6 text-center text-sm text-slate-500 bg-slate-50 dark:bg-slate-900">{error}</div>}
      <div className="absolute bottom-3 left-3 rounded-lg bg-white/90 dark:bg-slate-900/90 backdrop-blur px-2.5 py-1.5 text-[11px] text-slate-600 dark:text-slate-300 shadow">
        {visibleOverlayCount ? `${visibleOverlayCount} active map overlay${visibleOverlayCount === 1 ? '' : 's'}` : `Inventory coordinate · ${site.bridge.latitude.toFixed(5)}, ${site.bridge.longitude.toFixed(5)}`} · {viewMode.toUpperCase()}
      </div>
    </div>
  );
};

const BridgeSiteIntelligence: React.FC<{
  stateCode: string | number;
  bridgeNumber: string;
  dataYear?: string | number;
}> = ({ stateCode, bridgeNumber, dataYear }) => {
  const [site, setSite] = useState<SiteResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showTraffic, setShowTraffic] = useState(false);
  const [showTrafficCounts, setShowTrafficCounts] = useState(false);
  const [showWaterGages, setShowWaterGages] = useState(false);
  const [viewMode, setViewMode] = useState<'2d' | '3d'>('2d');
  const [terrainMetric, setTerrainMetric] = useState<TerrainMetric>('elevation_feet');
  const [selectedSurvey, setSelectedSurvey] = useState(0);
  const [imageryContent, setImageryContent] = useState('Vert');
  const [imagerySource, setImagerySource] = useState('nj-2020');
  const [activeLayerNames, setActiveLayerNames] = useState<string[]>([]);
  const [focusedLayer, setFocusedLayer] = useState<string | null>(null);
  const [layerData, setLayerData] = useState<Record<string, LayerResponse>>({});
  const [layerLoading, setLayerLoading] = useState<string[]>([]);
  const [layerErrors, setLayerErrors] = useState<Record<string, string>>({});

  const year = dataYear === undefined || dataYear === null || dataYear === '' ? undefined : Number(dataYear);
  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      setLoading(true); setError(null); setSite(null);
      setActiveLayerNames([]); setFocusedLayer(null); setLayerData({}); setLayerErrors({});
      try {
        const cached = await axios.get<SiteResponse>(`/api/site-intelligence/${stateCode}/${encodeURIComponent(bridgeNumber)}`, {
          params: year ? { year } : undefined, signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setSite(cached.data);
        const missingOrStale = SECTION_NAMES.filter((name) => {
          const item = cached.data.sections?.[name];
          return !item || item.status === 'missing' || item.cache_state === 'stale';
        });
        if (missingOrStale.length) {
          setRefreshing(true);
          const refreshed = await axios.post<SiteResponse>(
            `/api/site-intelligence/${stateCode}/${encodeURIComponent(bridgeNumber)}/refresh`,
            { year, sections: missingOrStale, force: false },
            { signal: controller.signal },
          );
          if (!controller.signal.aborted) setSite(refreshed.data);
        }
      } catch (exc: any) {
        if (!controller.signal.aborted && exc?.code !== 'ERR_CANCELED') {
          setError(exc?.response?.data?.detail || 'Site intelligence could not be loaded for this bridge.');
        }
      } finally {
        if (!controller.signal.aborted) { setLoading(false); setRefreshing(false); }
      }
    };
    load();
    return () => controller.abort();
  }, [stateCode, bridgeNumber, year]);

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true); setError(null);
    try {
      const response = await axios.post<SiteResponse>(
        `/api/site-intelligence/${stateCode}/${encodeURIComponent(bridgeNumber)}/refresh`,
        { year, sections: SECTION_NAMES, force: true },
      );
      setSite(response.data);
    } catch (exc: any) {
      setError(exc?.response?.data?.detail || 'The external sources could not be refreshed. Cached results remain visible.');
    } finally { setRefreshing(false); }
  };

  const toggleLayer = async (name: string) => {
    if (activeLayerNames.includes(name)) {
      const remaining = activeLayerNames.filter((item) => item !== name);
      setActiveLayerNames(remaining);
      if (focusedLayer === name) setFocusedLayer(remaining[remaining.length - 1] || null);
      return;
    }
    setActiveLayerNames((current) => [...current, name]);
    setFocusedLayer(name);
    setLayerErrors((current) => { const next = { ...current }; delete next[name]; return next; });
    if (layerData[name]) return;
    setLayerLoading((current) => [...current, name]);
    try {
      const response = await axios.get<LayerResponse>(
        `/api/site-intelligence/${stateCode}/${encodeURIComponent(bridgeNumber)}/layers/${encodeURIComponent(name)}`,
        { params: year ? { year } : undefined },
      );
      setLayerData((current) => ({ ...current, [name]: response.data }));
    } catch (exc: any) {
      setLayerErrors((current) => ({ ...current, [name]: exc?.response?.data?.detail || 'This map layer could not be loaded.' }));
    } finally { setLayerLoading((current) => current.filter((item) => item !== name)); }
  };

  const surveys = site?.sections?.imagery?.surveys || [];
  const publicImagery = site?.sections?.imagery?.public_imagery || [];
  const survey = surveys[selectedSurvey];
  const profileData = useMemo(() => {
    if (!site) return [];
    const terrain = site.sections.terrain;
    const ns = terrain?.profiles?.north_south?.samples || [];
    const ew = terrain?.profiles?.east_west?.samples || [];
    const byOffset = new Map<number, any>();
    ns.forEach((item: any) => byOffset.set(item.offset_feet, { offset: item.offset_feet, northSouth: item.elevation_feet }));
    ew.forEach((item: any) => byOffset.set(item.offset_feet, { ...(byOffset.get(item.offset_feet) || { offset: item.offset_feet }), eastWest: item.elevation_feet }));
    return [...byOffset.values()].sort((a, b) => a.offset - b.offset);
  }, [site]);
  const weatherChart = useMemo(() => (site?.sections.weather?.hourly || []).slice(0, 12).map((item: any) => ({
    time: item.startTime ? new Date(item.startTime).toLocaleTimeString([], { hour: 'numeric' }) : '',
    temperature: item.temperature,
  })), [site]);
  const trafficChart = useMemo(() => (site?.sections.traffic?.traffic_counts || []).slice(0, 8).map((item: any) => ({
    station: item.si_station_num || item.station_desc || 'Site',
    aadt: item.aadt,
  })).filter((item: any) => item.aadt != null), [site]);
  const tideChart = useMemo(() => (site?.sections.water?.coops_predictions || []).map((item: any) => ({
    time: item.t ? new Date(`${item.t.replace(' ', 'T')}Z`).toLocaleTimeString([], { hour: 'numeric' }) : '',
    level: Number(item.v),
  })).filter((item: any) => Number.isFinite(item.level)), [site]);
  const activeLayerData = useMemo(() => activeLayerNames.map((name) => layerData[name]).filter(Boolean), [activeLayerNames, layerData]);
  const focusedLayerData = focusedLayer ? layerData[focusedLayer] : null;

  if (loading && !site) return <div className="min-h-[420px] grid place-items-center"><div className="text-center"><Loader2 className="w-7 h-7 mx-auto animate-spin text-brand-500" /><p className="mt-2 text-sm text-slate-500">Loading bridge site context…</p></div></div>;
  if (!site) return <div className="max-w-xl mx-auto mt-10 rounded-2xl border border-red-200 bg-red-50 p-5 text-sm text-red-700"><AlertTriangle className="inline w-4 h-4 mr-2" />{error || 'Site intelligence is unavailable.'}</div>;

  const weather = site.sections.weather;
  const traffic = site.sections.traffic;
  const water = site.sections.water;
  const flood = site.sections.flood;
  const terrain = site.sections.terrain;
  const constraints = site.sections.constraints;
  const imagery = site.sections.imagery;
  const currentWeather = weather?.current;
  const nearestWater = water?.nearest_station || water?.nearest_nwps_gauge;
  const zones = flood?.flood_zones || [];
  const constraintLayers = constraints?.layers || {};
  const waterGageCount = (water?.nwps_gauges || []).length + (water?.nearest_station ? 1 : 0) + (water?.coops_station ? 1 : 0);
  const activeOverlayCount = activeLayerNames.length + Number(showTraffic) + Number(showTrafficCounts) + Number(showWaterGages);

  return (
    <div className="max-w-7xl mx-auto space-y-4 pb-4">
      <div className="rounded-2xl border border-indigo-100 dark:border-indigo-900/60 bg-gradient-to-br from-indigo-50 via-white to-sky-50 dark:from-indigo-950/40 dark:via-slate-900 dark:to-sky-950/30 p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="inline-flex items-center gap-1.5 rounded-full bg-white/80 dark:bg-slate-900/70 px-2.5 py-1 text-[11px] font-semibold text-brand-600 dark:text-brand-300 border border-indigo-100 dark:border-indigo-900">
              <MapPin className="w-3.5 h-3.5" /> Bridge-specific site screening
            </div>
            <h2 className="mt-2 text-xl font-bold text-slate-900 dark:text-white">{site.bridge.bridge_name || `Bridge ${site.bridge.bridge_number}`}</h2>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{site.bridge.bridge_location || 'New Jersey'} · inventory {site.bridge.data_year}</p>
          </div>
          <button onClick={refresh} disabled={refreshing} className="inline-flex items-center gap-2 rounded-xl border border-indigo-200 dark:border-indigo-800 bg-white/80 dark:bg-slate-900/70 px-3.5 py-2 text-sm font-semibold text-brand-600 dark:text-brand-300 hover:bg-white disabled:opacity-60">
            {refreshing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            {refreshing ? 'Refreshing sources…' : 'Refresh site data'}
          </button>
        </div>
        <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-2">
          <Fact label="Coordinates" value={`${site.bridge.latitude.toFixed(5)}, ${site.bridge.longitude.toFixed(5)}`} />
          <Fact label="Crossing" value={site.bridge.water_crossing ? (site.bridge.waterway_names?.join(', ') || 'Waterway') : 'Not identified as water'} />
          <Fact label="Deck condition" value={site.bridge.deck_condition_rating} />
          <Fact label="Scour condition" value={site.bridge.scour_condition_rating} />
        </div>
        {error && <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"><AlertTriangle className="inline w-3.5 h-3.5 mr-1" />{error}</div>}
      </div>

      <div className="grid sm:grid-cols-[240px_minmax(0,1fr)] xl:grid-cols-[310px_minmax(0,1fr)] gap-3 xl:gap-4 items-stretch">
        <aside className="h-[560px] overflow-hidden rounded-2xl border border-slate-200 bg-slate-50/80 dark:border-slate-700 dark:bg-slate-900">
          <div className="border-b border-slate-200 bg-white px-3 py-3 dark:border-slate-700 dark:bg-slate-900">
            <div className="flex items-center justify-between gap-2">
              <div><h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Map layers</h3><p className="text-[11px] text-slate-400">Select any combination</p></div>
              {activeOverlayCount > 0 && <button onClick={() => { setActiveLayerNames([]); setFocusedLayer(null); setShowTraffic(false); setShowTrafficCounts(false); setShowWaterGages(false); }} className="rounded-lg px-2 py-1 text-[11px] font-semibold text-indigo-600 hover:bg-indigo-50 dark:text-indigo-300 dark:hover:bg-indigo-950/40">Clear {activeOverlayCount}</button>}
            </div>
            <div className="mt-3 grid grid-cols-2 gap-1 rounded-xl bg-slate-100 p-1 dark:bg-slate-800">
              {(['2d', '3d'] as const).map((mode) => <button key={mode} onClick={() => setViewMode(mode)} className={`rounded-lg px-2 py-1.5 text-xs font-semibold transition ${viewMode === mode ? 'bg-white text-indigo-700 shadow-sm dark:bg-slate-700 dark:text-indigo-200' : 'text-slate-500'}`}>{mode === '2d' ? '2D map' : '3D terrain'}</button>)}
            </div>
            <label className="mt-3 block text-[10px] font-semibold uppercase tracking-wide text-slate-400">Basemap</label>
            <select value={imagerySource} onChange={(e) => setImagerySource(e.target.value)} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs dark:border-slate-700 dark:bg-slate-800">
              {publicImagery.map((item: any) => <option key={item.year} value={`nj-${item.year}`}>{item.label} · public</option>)}
              <option value="current">ArcGIS satellite</option>
              <option value="arcgis-topographic">ArcGIS topographic</option>
              <option value="osm">OpenStreetMap streets</option>
              {surveys.length > 0 && <option value="nearmap">Nearmap licensed survey</option>}
            </select>
            {imagerySource === 'nearmap' && surveys.length > 0 && <div className="mt-2 grid grid-cols-2 gap-1">
              <select value={selectedSurvey} onChange={(e) => setSelectedSurvey(Number(e.target.value))} className="min-w-0 rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-[11px] dark:border-slate-700 dark:bg-slate-800">{surveys.map((item: any, index: number) => <option key={item.survey_id || index} value={index}>{item.capture_date || `Survey ${index + 1}`}</option>)}</select>
              <select value={imageryContent} onChange={(e) => setImageryContent(e.target.value)} className="min-w-0 rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-[11px] dark:border-slate-700 dark:bg-slate-800">{['Vert', 'North', 'South', 'East', 'West'].map((item) => <option key={item}>{item}</option>)}</select>
            </div>}
          </div>
          <div className="h-[344px] overflow-y-auto overscroll-contain px-2 py-2">
            {MAP_LAYER_GROUPS.map((group) => <div key={group.key} className="mb-3 last:mb-0">
              <p className="px-1.5 pb-1 text-[10px] font-bold uppercase tracking-wide text-slate-400">{group.label}</p>
              <div className="space-y-0.5">{group.layers.map((name) => {
                if (name === 'traffic_count_sites') return <LayerToggle key={name} label="NJDOT traffic count sites" active={showTrafficCounts} count={(traffic?.traffic_counts || []).length} onClick={() => setShowTrafficCounts((value) => !value)} />;
                if (name === 'water_gages') return <LayerToggle key={name} label="Water gages" active={showWaterGages} count={waterGageCount} disabled={!waterGageCount} onClick={() => setShowWaterGages((value) => !value)} />;
                if (name === 'live_traffic') return <LayerToggle key={name} label="ArcGIS live traffic flow" active={showTraffic} disabled={!getArcgisApiKey()} onClick={() => setShowTraffic((value) => !value)} />;
                const layer = constraintLayers[name] || {};
                const isFema = name === 'fema_flood_zones' || name === 'base_flood_elevations';
                const count = name === 'fema_flood_zones' ? zones.length : name === 'base_flood_elevations' ? (flood?.base_flood_elevations || []).length : layer.status === 'available' ? (layer.features || []).length : null;
                return <LayerToggle key={name} label={pretty(name)} active={activeLayerNames.includes(name)} loading={layerLoading.includes(name)} error={layerErrors[name]} count={count} disabled={!isFema && layer.status !== 'available'} onClick={() => toggleLayer(name)} />;
              })}</div>
            </div>)}
          </div>
          <div className="border-t border-slate-200 bg-white px-3 py-2 text-[10px] leading-relaxed text-slate-400 dark:border-slate-700 dark:bg-slate-900">Geometry loads only when selected and is cached by bridge and layer.</div>
        </aside>
        <SiteMap site={site} showTraffic={showTraffic} showTrafficCounts={showTrafficCounts} showWaterGages={showWaterGages} survey={survey} content={imageryContent} imagerySource={imagerySource} viewMode={viewMode} activeLayers={activeLayerData} />
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <SectionCard title="Weather" icon={<CloudSun className="w-4 h-4" />} section={weather}>
          {currentWeather ? <>
            <div className="flex items-end gap-2"><span className="text-3xl font-bold text-slate-800 dark:text-white">{pretty(currentWeather.temperature)}°{currentWeather.temperatureUnit}</span><span className="pb-1 text-sm text-slate-500">{currentWeather.shortForecast}</span></div>
            <div className="mt-3 grid grid-cols-2 gap-2"><Fact label="Wind" value={`${currentWeather.windSpeed || 'N/A'} ${currentWeather.windDirection || ''}`} /><Fact label="Alerts" value={(weather.alerts || []).length} accent={(weather.alerts || []).length > 0} /></div>
            {weatherChart.length > 1 && <div className="mt-3 h-32"><ResponsiveContainer width="100%" height="100%"><LineChart data={weatherChart} margin={{ top: 5, right: 6, bottom: 0, left: -24 }}><CartesianGrid strokeDasharray="3 3" opacity={0.25} /><XAxis dataKey="time" tick={{ fontSize: 9 }} /><YAxis tick={{ fontSize: 9 }} /><Tooltip /><Line type="monotone" dataKey="temperature" name={`Temperature (°${currentWeather.temperatureUnit})`} stroke="#4f46e5" strokeWidth={2} dot={false} /></LineChart></ResponsiveContainer></div>}
          </> : <EmptyState section={weather} />}
        </SectionCard>
        <SectionCard title="Traffic" icon={<Navigation className="w-4 h-4" />} section={traffic}>
          {traffic?.status === 'available' ? <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2"><Fact label="Nearest AADT" value={traffic.nearest_count?.aadt} /><Fact label="Count distance" value={traffic.nearest_count?.distance_miles == null ? null : `${traffic.nearest_count.distance_miles.toFixed(1)} mi`} /><Fact label="Public count sites" value={(traffic.traffic_counts || []).length} /><Fact label="Live flow" value={traffic.map_available ? 'Available' : 'Optional'} /></div>
            {traffic.nearest_count && <p className="text-[11px] text-slate-400">NJDOT station {traffic.nearest_count.si_station_num} · {String(traffic.nearest_count.count_year || '').slice(0, 4)} count</p>}
            {trafficChart.length > 1 && <div className="h-32"><ResponsiveContainer width="100%" height="100%"><BarChart data={trafficChart} margin={{ top: 5, right: 4, bottom: 0, left: -22 }}><CartesianGrid strokeDasharray="3 3" opacity={0.25} /><XAxis dataKey="station" tick={{ fontSize: 8 }} /><YAxis tick={{ fontSize: 9 }} /><Tooltip /><Bar dataKey="aadt" name="AADT" fill="#0ea5e9" radius={[3, 3, 0, 0]} /></BarChart></ResponsiveContainer></div>}
            {traffic.camera_map_url && <a href={traffic.camera_map_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 rounded-lg bg-sky-50 px-2.5 py-1.5 text-xs font-semibold text-sky-700 hover:bg-sky-100 dark:bg-sky-950/40 dark:text-sky-300"><Camera className="w-3.5 h-3.5" />Open current 511NJ cameras <ExternalLink className="w-3 h-3" /></a>}
          </div> : <EmptyState section={traffic} />}
        </SectionCard>
      </div>

      <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
        <SectionCard title="Waterway & gages" icon={<Waves className="w-4 h-4" />} section={water}>
          {nearestWater || water?.coops_station ? <div className="space-y-3">
            {nearestWater && <><p className="text-sm font-semibold text-slate-700 dark:text-slate-200">{nearestWater.station_name || nearestWater.name}</p><div className="grid grid-cols-2 gap-2"><Fact label="River reading" value={nearestWater.value != null ? `${pretty(nearestWater.value)} ${nearestWater.unit || ''}` : (nearestWater.observed_status?.primary != null ? `${pretty(nearestWater.observed_status.primary)} ${nearestWater.observed_status.primaryUnit || ''}` : 'Gauge available')} /><Fact label="Distance" value={nearestWater.distance_miles == null ? null : `${nearestWater.distance_miles.toFixed(1)} mi`} /></div></>}
            {water?.coops_station && <div className="rounded-xl border border-sky-100 dark:border-sky-900/60 bg-sky-50/60 dark:bg-sky-950/20 p-3"><p className="text-[10px] font-semibold uppercase tracking-wide text-sky-600">Coastal / tidal station</p><p className="mt-1 text-xs font-semibold text-slate-700 dark:text-slate-200">{water.coops_station.station_name}</p><p className="mt-1 text-[11px] text-slate-500">{water.coops_latest?.value != null ? `${water.coops_latest.value} ${water.coops_latest.unit}` : 'Station metadata available'} · {water.coops_station.distance_miles.toFixed(1)} mi</p></div>}
            {tideChart.length > 1 && <div className="h-36"><ResponsiveContainer width="100%" height="100%"><LineChart data={tideChart} margin={{ top: 5, right: 6, bottom: 0, left: -20 }}><CartesianGrid strokeDasharray="3 3" opacity={0.25} /><XAxis dataKey="time" tick={{ fontSize: 9 }} /><YAxis tick={{ fontSize: 9 }} unit=" ft" /><Tooltip /><Line type="monotone" dataKey="level" name="Predicted tide (ft MLLW)" stroke="#0284c7" strokeWidth={2} dot={false} /></LineChart></ResponsiveContainer></div>}
            {water?.streamstats_url && <a href={water.streamstats_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs font-semibold text-brand-600 hover:underline">Open USGS watershed analysis <ExternalLink className="w-3 h-3" /></a>}
          </div> : <EmptyState section={water} />}
        </SectionCard>
        <SectionCard title="Flood screening" icon={<ShieldCheck className="w-4 h-4" />} section={flood}>
          {zones.length ? <div className="space-y-2">{zones.slice(0, 3).map((zone: any, index: number) => <div key={index} className="rounded-xl bg-slate-50 dark:bg-slate-800/60 p-3"><p className="text-xs font-semibold text-slate-700 dark:text-slate-200">Flood zone {zone.FLD_ZONE || zone.ZONE_SUBTY || zone.DFIRM_ID || index + 1}</p><p className="mt-1 text-[11px] text-slate-500">{zone.ZONE_SUBTY || zone.SFHA_TF || 'Published FEMA NFHL intersection'}</p></div>)}</div> : <EmptyState section={flood} />}
        </SectionCard>
        <SectionCard title="Imagery" icon={<Camera className="w-4 h-4" />} section={imagery}>
          <div className="grid grid-cols-2 gap-2"><Fact label="Public NJ orthos" value={publicImagery.length} /><Fact label="Nearmap surveys" value={surveys.length || 'Optional'} /></div>
          <p className="mt-3 text-xs text-slate-500">2012, 2015, and 2020 statewide orthophotos can be compared without credentials.{surveys.length ? ` Latest Nearmap: ${surveys[0]?.capture_date || 'date not reported'}.` : ''}</p>
        </SectionCard>
      </div>

      <SectionCard title="Terrain & approach slopes" icon={<Mountain className="w-4 h-4" />} section={terrain}>
        {profileData.length && terrain.grid?.samples?.length ? <div className="space-y-4">
          <div className="grid lg:grid-cols-[340px_minmax(0,1fr)] gap-5 items-center">
            <TerrainGrid grid={terrain.grid} metric={terrainMetric} onMetricChange={setTerrainMetric} />
            <div>
              <div className="mb-1 flex items-center justify-between gap-3"><p className="text-xs font-semibold text-slate-700 dark:text-slate-200">Centerline approach profiles</p><span className="text-[10px] text-slate-400">Bridge at 0 ft</span></div>
              <div className="h-64"><ResponsiveContainer width="100%" height="100%"><LineChart data={profileData} margin={{ top: 10, right: 15, left: 0, bottom: 5 }}><CartesianGrid strokeDasharray="3 3" stroke="#cbd5e1" opacity={0.45} /><XAxis dataKey="offset" tick={{ fontSize: 11 }} label={{ value: 'Offset from bridge (ft)', position: 'insideBottom', offset: -2, fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} unit=" ft" /><Tooltip /><Legend /><Line type="monotone" dataKey="northSouth" name="North–south" stroke="#4f46e5" strokeWidth={2} dot={false} /><Line type="monotone" dataKey="eastWest" name="East–west" stroke="#0ea5e9" strokeWidth={2} dot={false} /></LineChart></ResponsiveContainer></div>
            </div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Fact label="Grid maximum slope" value={terrain.grid.maximum_slope_percent == null ? null : `${terrain.grid.maximum_slope_percent.toFixed(1)}%`} />
            <Fact label="Grid average slope" value={terrain.grid.average_slope_percent == null ? null : `${terrain.grid.average_slope_percent.toFixed(1)}%`} />
            <Fact label="N–S max grade" value={terrain.profiles?.north_south?.maximum_segment_grade_percent == null ? null : `${terrain.profiles.north_south.maximum_segment_grade_percent.toFixed(1)}%`} />
            <Fact label="E–W max grade" value={terrain.profiles?.east_west?.maximum_segment_grade_percent == null ? null : `${terrain.profiles.east_west.maximum_segment_grade_percent.toFixed(1)}%`} />
          </div>
          <p className="text-[11px] leading-relaxed text-slate-400">{terrain.method_note}</p>
        </div> : <EmptyState section={terrain} />}
      </SectionCard>

      <div className="rounded-xl border border-amber-100 bg-amber-50/60 px-3 py-2.5 text-xs text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-300">
        <AlertTriangle className="inline w-3.5 h-3.5 mr-1.5" />{constraints?.method_note || 'Screening results must be verified before design or construction decisions.'}
      </div>

      {focusedLayer && <SectionCard title={`${pretty(focusedLayer)} · map analysis`} icon={layerLoading.includes(focusedLayer) ? <Loader2 className="w-4 h-4 animate-spin" /> : <Layers3 className="w-4 h-4" />} section={constraints}>
        {layerErrors[focusedLayer] ? <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700">{layerErrors[focusedLayer]}</div> : layerLoading.includes(focusedLayer) || !focusedLayerData ? <div className="h-44 grid place-items-center text-sm text-slate-400"><Loader2 className="w-5 h-5 animate-spin mr-2" />Loading selected geometry…</div> : <div className="space-y-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Fact label="Mapped features" value={focusedLayerData.features.length} />
            <Fact label="Geometry" value={pretty(focusedLayerData.geometry_type?.replace('esriGeometry', '') || 'Not reported')} />
            <Fact label="Search radius" value={focusedLayerData.search_radius_miles == null ? 'At bridge' : `${focusedLayerData.search_radius_miles} mi`} />
            <Fact label="Cached" value={focusedLayerData.cache_state === 'fresh' ? 'Yes' : 'Refreshing'} />
          </div>
          {focusedLayerData.summaries?.[0]?.values?.length > 1 && <div>
            <p className="mb-2 text-xs font-semibold text-slate-600 dark:text-slate-300">Features by {focusedLayerData.summaries[0].label}</p>
            <div className="h-52"><ResponsiveContainer width="100%" height="100%"><BarChart data={focusedLayerData.summaries[0].values} layout="vertical" margin={{ top: 4, right: 20, bottom: 4, left: 42 }}><CartesianGrid strokeDasharray="3 3" opacity={0.25} /><XAxis type="number" allowDecimals={false} tick={{ fontSize: 10 }} /><YAxis type="category" dataKey="label" width={100} tick={{ fontSize: 9 }} /><Tooltip /><Bar dataKey="count" name="Features" fill="#4f46e5" radius={[0, 4, 4, 0]} /></BarChart></ResponsiveContainer></div>
          </div>}
          {focusedLayerData.features.length > 0 && (() => {
            const fields = orderedFields(focusedLayerData.fields, focusedLayerData.features).slice(0, 6);
            return <div>
              <div className="flex items-center justify-between gap-3 mb-2"><p className="text-xs font-semibold text-slate-600 dark:text-slate-300">Attribute grid</p><span className="text-[11px] text-slate-400">Showing up to 20 of {focusedLayerData.features.length}</span></div>
              <div className="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-700">
                <table className="w-full min-w-[640px] text-xs">
                  <thead className="bg-slate-50 dark:bg-slate-800"><tr>{fields.map((field) => <th key={field.name} className="px-3 py-2 text-left font-semibold text-slate-500">{field.alias || pretty(field.name)}</th>)}</tr></thead>
                  <tbody className="divide-y divide-slate-100 dark:divide-slate-800">{focusedLayerData.features.slice(0, 20).map((feature, index) => <tr key={index}>{fields.map((field) => <td key={field.name} className="max-w-[240px] truncate px-3 py-2 text-slate-600 dark:text-slate-300" title={String(feature.attributes?.[field.name] ?? '')}>{displayAttribute(field, feature.attributes?.[field.name])}</td>)}</tr>)}</tbody>
                </table>
              </div>
            </div>;
          })()}
          {focusedLayer === 'archaeological_site_grid' && <p className="rounded-xl bg-indigo-50 px-3 py-2 text-xs text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300">This layer provides mapped grid cells and status attributes. It does not contain site photographs or video.</p>}
          {focusedLayerData.exceeded_transfer_limit && <p className="text-xs text-amber-700">The source returned its first 100 nearby features. Refine the map area before using the grid as a complete inventory.</p>}
          <div className="flex items-center justify-between gap-3 text-[11px] text-slate-400"><span>Loaded only after this layer was selected · updated {dateTime(focusedLayerData.fetched_at)}</span><a href={focusedLayerData.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-brand-600 hover:underline">Layer source <ExternalLink className="w-3 h-3" /></a></div>
        </div>}
      </SectionCard>}
    </div>
  );
};

export default BridgeSiteIntelligence;
