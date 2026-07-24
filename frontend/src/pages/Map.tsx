// frontend/src/pages/Map.tsx
// Map Explorer (opens as a full-bleed modal): ArcGIS map with floating glass
// controls, county drilldown, and 6-action bridge popups.
import React, { useState, useEffect, useMemo, useRef } from 'react';
import axios from 'axios';
import { MapPin, Loader2, Layers, Filter, Satellite, Map as MapIcon } from 'lucide-react';
import ArcgisMap, { MapBridge, MapMode, BaseLayer, BridgeAction, PopupBridgeRef, MapBridgeFocus } from '../components/ArcgisMap';
import BridgeHistoryModal from '../components/BridgeHistoryModal';
import BridgeReportsModal from '../components/BridgeReportsModal';
import BridgeLCCAModal from '../components/BridgeLCCAModal';
import BridgeDetailModal from '../components/BridgeDetailModal';
import { openTool } from '../lib/tools';
import {
  SHOW_BRIDGE_ON_MAP_EVENT,
  emitShowBridgeOnMapResult,
  type ShowBridgeOnMapRequest,
} from '../lib/mapBridgeEvents';

interface StateOption { state_code: number; state_name: string; }
interface CountyOption { state_code: number; county_code: number; bridge_count: number; }

const CONDITION_FEATURES = [
  { key: 'deck_condition_rating', label: 'Deck' },
  { key: 'superstructure_condition_rating', label: 'Superstructure' },
  { key: 'substructure_condition_rating', label: 'Substructure' },
  { key: 'culvert_condition_rating', label: 'Culvert' },
  { key: 'channel_condition_rating', label: 'Channel' },
  { key: 'scour_condition_rating', label: 'Scour' },
  { key: 'bridge_railings_condition_rating', label: 'Railings' },
  { key: 'bridge_bearings_condition_rating', label: 'Bearings' },
  { key: 'bridge_joints_condition_rating', label: 'Joints' },
];

// NJ county FIPS (3-digit) → name, for the county dropdown.
const NJ_COUNTY_NAMES: Record<number, string> = {
  1: 'Atlantic', 3: 'Bergen', 5: 'Burlington', 7: 'Camden', 9: 'Cape May',
  11: 'Cumberland', 13: 'Essex', 15: 'Gloucester', 17: 'Hudson', 19: 'Hunterdon',
  21: 'Mercer', 23: 'Middlesex', 25: 'Monmouth', 27: 'Morris', 29: 'Ocean',
  31: 'Passaic', 33: 'Salem', 35: 'Somerset', 37: 'Sussex', 39: 'Union', 41: 'Warren',
};
const countyName = (code: number) => NJ_COUNTY_NAMES[code] || `County ${String(code).padStart(3, '0')}`;

const LEGEND = [
  { color: '#22c55e', label: 'Good (7–9)' },
  { color: '#f59e0b', label: 'Fair (5–6)' },
  { color: '#ef4444', label: 'Poor (0–4)' },
  { color: '#7f1d1d', label: 'Critical' },
];

const selectClass =
  'bg-white/90 dark:bg-slate-800/90 border border-slate-300 dark:border-slate-700 rounded-xl px-3 py-2 text-sm text-slate-800 dark:text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-400';

const MapPage: React.FC = () => {
  const [allBridges, setAllBridges] = useState<MapBridge[]>([]);
  const [loadingOptions, setLoadingOptions] = useState(true);
  const [loadingBridges, setLoadingBridges] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [availableStates, setAvailableStates] = useState<StateOption[]>([]);
  const [availableYears, setAvailableYears] = useState<number[]>([]);
  const [availableCounties, setAvailableCounties] = useState<CountyOption[]>([]);
  const [selectedState, setSelectedState] = useState<string>('');
  const [selectedCounty, setSelectedCounty] = useState<string>('');
  const [selectedYear, setSelectedYear] = useState<string>('');

  const [mode] = useState<MapMode>('markers');
  const [baseLayer, setBaseLayer] = useState<BaseLayer>('streets');
  const [feature, setFeature] = useState<string>('deck_condition_rating');

  const [historyModal, setHistoryModal] = useState<PopupBridgeRef | null>(null);
  const [reportsModal, setReportsModal] = useState<PopupBridgeRef | null>(null);
  const [lccaModal, setLccaModal] = useState<PopupBridgeRef | null>(null);
  const [detailBridge, setDetailBridge] = useState<PopupBridgeRef | null>(null);
  const [focusBridge, setFocusBridge] = useState<MapBridgeFocus | null>(null);
  const focusTargetRef = useRef<MapBridge | null>(null);
  const desiredCountyRef = useRef<string | null>(null);
  const searchableBridgesRef = useRef<MapBridge[] | null>(null);

  const handleBridgeAction = (action: BridgeAction, bridge: PopupBridgeRef) => {
    const payload = { stateCode: bridge.state_code, bridgeNumber: bridge.bridge_number, bridgeName: bridge.bridge_name };
    if (action === 'open') setDetailBridge(bridge);
    else if (action === 'history') setHistoryModal(bridge);
    else if (action === 'reports') setReportsModal(bridge);
    else if (action === 'lcca') setLccaModal(bridge);
    else if (action === 'edit') openTool('editor', payload);
    else if (action === 'explorer') openTool('explorer', payload);
    else if (action === 'pointcloud') openTool('pointcloud', payload);
  };

  useEffect(() => {
    const fetchOptions = async () => {
      try {
        const [statesRes, yearsRes] = await Promise.all([
          axios.get('/api/states'),
          axios.get('/api/loaded-years'),
        ]);
        const states: StateOption[] = statesRes.data || [];
        setAvailableStates(states);
        const years = yearsRes.data?.years || [];
        setAvailableYears(years);
        if (years.length) setSelectedYear(String(years[0]));
        // Default to New Jersey (FIPS 34) so the landing loads one real county
        // immediately instead of rendering the whole inventory at once.
        const nj = states.find((s) => s.state_code === 34 || /new jersey/i.test(s.state_name || ''));
        if (nj) setSelectedState(String(nj.state_code));
      } catch (err) {
        console.error(err);
        setError('Unable to load map filter options');
      } finally {
        setLoadingOptions(false);
      }
    };
    fetchOptions();
  }, []);

  useEffect(() => {
    if (!selectedState) {
      setAvailableCounties([]); setSelectedCounty(''); setAllBridges([]);
      return;
    }
    const fetchCounties = async () => {
      try {
        const params = new URLSearchParams({ state_code: selectedState });
        if (selectedYear) params.set('year', selectedYear);
        const res = await axios.get(`/api/map-counties?${params.toString()}`);
        const counties: CountyOption[] = res.data || [];
        setAvailableCounties(counties);
        // Auto-select the first county so exactly one county loads at a time.
        const desired = desiredCountyRef.current;
        const desiredExists = desired && counties.some((county) => String(county.county_code) === desired);
        setSelectedCounty(desiredExists ? desired : (counties.length ? String(counties[0].county_code) : ''));
        setAllBridges([]);
      } catch (err) {
        console.error(err);
        setError('Unable to load county options');
      }
    };
    fetchCounties();
  }, [selectedState, selectedYear]);

  useEffect(() => {
    const normalize = (value: unknown) => String(value ?? '')
      .toUpperCase()
      .replace(/[^A-Z0-9]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

    const onShowBridge = async (event: Event) => {
      const request = (event as CustomEvent<ShowBridgeOnMapRequest>).detail;
      if (!request?.requestId || !request.searchText) return;
      try {
        if (!searchableBridgesRef.current) {
          const response = await axios.get('/api/map-data?state_code=34');
          const normalizedBridges = (response.data || [])
            .map((bridge: MapBridge) => ({
              ...bridge,
              latitude: Number(bridge.latitude),
              longitude: Number(bridge.longitude),
            }))
            .filter((bridge: MapBridge) => Number.isFinite(bridge.latitude) && Number.isFinite(bridge.longitude));
          const uniqueBridges = new Map<string, MapBridge>();
          normalizedBridges.forEach((bridge: MapBridge) => {
            uniqueBridges.set(`${bridge.state_code ?? 34}|${bridge.bridge_number}`, bridge);
          });
          searchableBridgesRef.current = Array.from(uniqueBridges.values());
        }

        const needle = normalize(request.searchText);
        const tokens = needle.split(' ').filter(Boolean);
        const ranked = (searchableBridgesRef.current || []).map((bridge) => {
          const number = normalize(bridge.bridge_number);
          const name = normalize(bridge.bridge_name || bridge.display_name);
          const location = normalize(bridge.bridge_location);
          const combined = `${number} ${name} ${location}`.trim();
          let score = 0;
          if (number === needle) score = 100;
          else if (number.includes(needle) && needle.length >= 3) score = 88;
          else if (name === needle) score = 80;
          else if (name.includes(needle) && needle.length >= 4) score = 70;
          else if (location.includes(needle) && needle.length >= 4) score = 60;
          else if (tokens.length && tokens.every((token) => combined.includes(token))) score = 50 + tokens.length;
          return { bridge, score };
        }).filter((item) => item.score > 0).sort((a, b) => b.score - a.score);

        if (!ranked.length) {
          emitShowBridgeOnMapResult({
            requestId: request.requestId,
            status: 'not_found',
            message: `I couldn't find a mapped New Jersey bridge matching “${request.searchText}”. Try its structure number.`,
          });
          return;
        }

        const bestScore = ranked[0].score;
        const equallyRanked = ranked.filter((item) => item.score === bestScore);
        if (bestScore < 88 && equallyRanked.length > 1) {
          const matches = equallyRanked.slice(0, 4).map(({ bridge }) => ({
            bridge_number: String(bridge.bridge_number),
            label: bridge.bridge_name || bridge.display_name || bridge.bridge_location || `Bridge ${bridge.bridge_number}`,
          }));
          emitShowBridgeOnMapResult({
            requestId: request.requestId,
            status: 'ambiguous',
            message: `I found multiple bridges: ${matches.map((match) => `${match.bridge_number} (${match.label})`).join(', ')}. Ask again with the structure number.`,
            matches,
          });
          return;
        }

        const target = ranked[0].bridge;
        const county = String(target.county_code ?? '');
        const state = String(target.state_code ?? 34);
        const year = target.data_year == null ? '' : String(target.data_year);
        desiredCountyRef.current = county;
        focusTargetRef.current = target;
        setFocusBridge({
          requestId: request.requestId,
          state_code: state,
          bridge_number: String(target.bridge_number),
        });
        setSelectedState(state);
        if (year) setSelectedYear(year);
        if (county) setSelectedCounty(county);
      } catch (err: any) {
        emitShowBridgeOnMapResult({
          requestId: request.requestId,
          status: 'error',
          message: err?.message || 'Could not search the bridge map.',
        });
      }
    };

    window.addEventListener(SHOW_BRIDGE_ON_MAP_EVENT, onShowBridge as EventListener);
    return () => window.removeEventListener(SHOW_BRIDGE_ON_MAP_EVENT, onShowBridge as EventListener);
  }, []);

  useEffect(() => {
    if (!selectedState || !selectedCounty) { setAllBridges([]); return; }
    const fetchBridgeFeatures = async () => {
      setLoadingBridges(true);
      try {
        const params = new URLSearchParams({ state_code: selectedState, county_code: selectedCounty });
        if (selectedYear) params.set('year', selectedYear);
        const res = await axios.get(`/api/map-data?${params.toString()}`);
        setAllBridges(res.data || []);
      } catch (err) {
        console.error(err);
        setError('Unable to load bridge data for the selected county');
      } finally {
        setLoadingBridges(false);
      }
    };
    fetchBridgeFeatures();
  }, [selectedState, selectedCounty, selectedYear]);

  const validBridges = useMemo(() =>
    allBridges
      .map((b) => ({ ...b, latitude: Number(b.latitude), longitude: Number(b.longitude) }))
      .filter((b) => !Number.isNaN(b.latitude) && !Number.isNaN(b.longitude) && b.latitude >= 17 && b.latitude <= 72 && b.longitude >= -180 && b.longitude <= -65),
    [allBridges]);

  const filteredBridges = useMemo(() => {
    const byBridge = new Map<string, MapBridge>();
    for (const bridge of validBridges) byBridge.set(`${bridge.state_code ?? ''}|${bridge.county_code ?? ''}|${bridge.bridge_number}`, bridge);
    return Array.from(byBridge.values());
  }, [validBridges]);

  if (loadingOptions) {
    return <div className="flex items-center justify-center h-full min-h-[400px]"><Loader2 className="w-12 h-12 animate-spin text-brand-500" /></div>;
  }
  if (error) {
    return (
      <div className="flex items-center justify-center h-full min-h-[400px] p-8 text-center text-red-500">
        <div><p className="text-2xl font-semibold mb-3">Map load failed</p><p>{error}</p></div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-h-[520px]">
      {/* Floating glass control bar */}
      <div className="shrink-0 flex flex-wrap items-center gap-2.5 px-4 py-3 border-b border-slate-200/60 dark:border-slate-700/60">
        <div className="flex items-center gap-2 mr-auto min-w-0">
          <span className="grid place-items-center w-9 h-9 rounded-xl bg-rose-500/10 text-rose-500 shrink-0"><MapPin className="w-5 h-5" /></span>
          <div className="min-w-0">
            <div className="font-semibold text-slate-800 dark:text-slate-100 leading-tight">New Jersey Bridge Inventory</div>
            <div className="text-xs text-slate-400 truncate">{selectedCounty ? `${filteredBridges.length.toLocaleString()} bridges rendered` : 'select a county'}</div>
          </div>
        </div>

        <span className="hidden sm:inline-flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium bg-rose-500/10 text-rose-600 dark:text-rose-300 border border-rose-300/40">
          <Layers className="w-4 h-4" /> New Jersey
        </span>
        <div className="flex items-center gap-1.5">
          <Filter className="w-4 h-4 text-slate-400" />
          <select value={selectedCounty} onChange={(e) => setSelectedCounty(e.target.value)} disabled={!selectedState} className={`${selectClass} disabled:opacity-50`} style={{ minWidth: 170 }}>
            <option value="">Select County</option>
            {[...availableCounties].sort((a, b) => countyName(a.county_code).localeCompare(countyName(b.county_code))).map((c) => <option key={`${c.state_code}-${c.county_code}`} value={String(c.county_code)}>{countyName(c.county_code)} ({c.bridge_count.toLocaleString()})</option>)}
          </select>
        </div>
        <select value={selectedYear} onChange={(e) => setSelectedYear(e.target.value)} className={selectClass} style={{ minWidth: 110 }}>
          <option value="">Latest Year</option>
          {availableYears.map((y) => <option key={y} value={String(y)}>{y}</option>)}
        </select>

        <button onClick={() => setBaseLayer((l) => (l === 'streets' ? 'satellite' : 'streets'))} className="inline-flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium border border-emerald-300 dark:border-emerald-700 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-50 dark:hover:bg-emerald-950/40 transition-colors">
          {baseLayer === 'streets' ? <Satellite className="w-4 h-4" /> : <MapIcon className="w-4 h-4" />}
          {baseLayer === 'streets' ? 'Satellite' : 'Streets'}
        </button>

        <select value={feature} onChange={(e) => setFeature(e.target.value)} className={`${selectClass} border-brand-300 dark:border-brand-700`} style={{ minWidth: 150 }} title="Color markers by condition">
          {CONDITION_FEATURES.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
        </select>
      </div>

      {/* Map area */}
      <div className="relative flex-1 min-h-0">
        <div className="absolute top-4 right-4 z-[600] glass-strong rounded-2xl shadow-lg px-4 py-3">
          <div className="font-semibold text-xs text-slate-700 dark:text-slate-100 mb-2">Condition Legend</div>
          {LEGEND.map((l) => (
            <div key={l.label} className="flex items-center gap-2.5 mb-1.5 last:mb-0">
              <span className="inline-block w-5 h-3.5 rounded" style={{ background: l.color }} />
              <span className="text-xs font-medium text-slate-600 dark:text-slate-300">{l.label}</span>
            </div>
          ))}
        </div>

        {selectedState ? (
          <>
            <ArcgisMap
              bridges={filteredBridges}
              mode={mode}
              feature={feature}
              basemap={baseLayer}
              className="h-full w-full"
              center={[-74.5, 40.1]}
              zoom={8}
              showCounties
              onCountySelect={(c) => setSelectedCounty(c)}
              onBridgeAction={handleBridgeAction}
              focusBridge={focusBridge}
              onFocusResult={(focus, success, focusError) => {
                const target = focusTargetRef.current;
                if (success && target) {
                  const label = target.bridge_name || target.display_name || `Bridge ${target.bridge_number}`;
                  emitShowBridgeOnMapResult({
                    requestId: focus.requestId,
                    status: 'shown',
                    message: `Showing ${label} (Bridge ${target.bridge_number}) on the map.`,
                    bridge: target,
                  });
                } else {
                  emitShowBridgeOnMapResult({
                    requestId: focus.requestId,
                    status: 'error',
                    message: focusError || 'The bridge was found, but its map card could not be opened.',
                  });
                }
                desiredCountyRef.current = null;
                focusTargetRef.current = null;
                setFocusBridge(null);
              }}
            />
            {loadingBridges && (
              <div className="absolute inset-0 flex items-center justify-center bg-white/50 dark:bg-slate-950/50 z-[650]">
                <div className="flex items-center gap-3 rounded-2xl glass-strong px-5 py-3 shadow-lg">
                  <Loader2 className="w-5 h-5 animate-spin text-brand-500" />
                  <span className="text-sm font-medium text-slate-700 dark:text-slate-200">Loading county bridges…</span>
                </div>
              </div>
            )}
          </>
        ) : (
          <div className="h-full flex items-center justify-center text-center p-8 bg-slate-100 dark:bg-slate-900">
            <div>
              <MapPin className="w-12 h-12 text-rose-500 mx-auto mb-4" />
              <p className="text-2xl font-semibold text-slate-900 dark:text-white mb-2">Select a state and county</p>
              <p className="text-slate-500 dark:text-slate-400 max-w-xl">Bridge features load one county at a time so ArcGIS does not render the full multi-year inventory at once.</p>
            </div>
          </div>
        )}
      </div>

      {historyModal && (
        <BridgeHistoryModal stateCode={historyModal.state_code} bridgeNumber={historyModal.bridge_number} bridgeName={historyModal.bridge_name} onClose={() => setHistoryModal(null)} />
      )}
      {reportsModal && (
        <BridgeReportsModal stateCode={reportsModal.state_code} bridgeNumber={reportsModal.bridge_number} bridgeName={reportsModal.bridge_name} onClose={() => setReportsModal(null)} />
      )}
      {lccaModal && (
        <BridgeLCCAModal
          stateCode={lccaModal.state_code}
          bridgeName={lccaModal.bridge_name}
          bridgeNumber={lccaModal.bridge_number}
          yearBuilt={lccaModal.year_built}
          onClose={() => setLccaModal(null)}
        />
      )}
      {detailBridge && (
        <BridgeDetailModal bridge={detailBridge} onClose={() => setDetailBridge(null)} />
      )}
    </div>
  );
};

export default MapPage;
