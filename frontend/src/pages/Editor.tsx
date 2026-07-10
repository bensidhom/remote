// frontend/src/pages/Editor.tsx

import React, { useState, useEffect } from 'react';
import axios from 'axios';
import JSZip from 'jszip';
import { Edit3, Save, Plus, Loader2, Download } from 'lucide-react';
import { SectionHeader } from '../components/ui';

interface Bridge {
  state_code: number;
  bridge_number: string;
  bridge_name?: string;
  /** 1 when legacy Item 42A marks a non-highway bridge FHWA says to omit from the SNBI transition. */
  fhwa_omit?: number;
  [key: string]: any;
}

interface TableRow {
  [key: string]: any;
}

interface ValidationIssue {
  type: 'critical' | 'error' | 'flag' | 'safety';
  bridge: string;
  rule: string;
  tag: string;
  message: string;
  table?: string | null;
  row_index?: number | null;
  expected?: string;
  editable_hint?: string;
}

interface FieldMetadata {
  table: string;
  row_index?: number | null;
  tag: string;
  source_name?: string | null;
  source_value?: string | null;
  note?: string | null;
  is_temporary?: boolean;
  is_default?: boolean;
  requires_review?: boolean;
  rule?: string | null;
  transition?: string | null;
}

const CHILD_TABLES = [
  'features',
  'routes',
  'span_sets',
  'substructure_sets',
  'elements',
  'inspections',
  'posting_evaluations',
  'posting_statuses',
  'works',
];

const SNBI_MAIN_FIELDS = [
  'BL01', 'BID01', 'BID02', 'BID03', 'BL02', 'BL03', 'BL04', 'BL05', 'BL06', 'BL07', 'BL08', 'BL09', 'BL10',
  'BL11', 'BL12', 'BCL01', 'BCL02', 'BCL03', 'BCL04', 'BCL05', 'BCL06', 'BRH01', 'BRH02', 'BG01', 'BG02',
  'BG03', 'BG04', 'BG05', 'BG06', 'BG07', 'BG08', 'BG09', 'BG10', 'BG11', 'BG12', 'BG13', 'BG14', 'BG15',
  'BG16', 'BLR01', 'BLR02', 'BLR03', 'BLR04', 'BLR05', 'BLR06', 'BLR07', 'BLR08', 'BIR01', 'BIR02', 'BIR03',
  'BIR04', 'BC01', 'BC02', 'BC03', 'BC04', 'BC05', 'BC06', 'BC07', 'BC08', 'BC09', 'BC10', 'BC11', 'BC12',
  'BC13', 'BC14', 'BC15', 'BAP01', 'BAP02', 'BAP03', 'BAP04', 'BAP05', 'BW01'
];

const INTERNAL_EXPORT_KEYS = new Set(['id', 'rowid', 'state_code', 'bridge_number', 'data_year']);

const COLUMN_TO_SNBI_KEY: Record<string, string> = {
  state_code: 'BL01',
  bridge_number: 'BID01',
  bridge_name: 'BID02',
  previous_bridge_number: 'BID03',
  county_code: 'BL02',
  place_code: 'BL03',
  highway_agency_district: 'BL04',
  latitude: 'BL05',
  longitude: 'BL06',
  border_bridge_number: 'BL07',
  border_bridge_state_or_country_code: 'BL08',
  border_bridge_inspection_responsibility: 'BL09',
  border_bridge_designated_lead_state: 'BL10',
  bridge_location: 'BL11',
  metropolitan_planning_organization: 'BL12',
  owner: 'BCL01',
  maintenance_responsibility: 'BCL02',
  federal_or_tribal_land_access: 'BCL03',
  historical_significance: 'BCL04',
  toll: 'BCL05',
  emergency_evacuation_designation: 'BCL06',
  bridge_railings: 'BRH01',
  transitions: 'BRH02',
  nbis_bridge_length: 'BG01',
  total_bridge_length: 'BG02',
  maximum_span_length: 'BG03',
  minimum_span_length: 'BG04',
  bridge_width_out_to_out: 'BG05',
  bridge_width_curb_to_curb: 'BG06',
  left_curb_or_sidewalk_width: 'BG07',
  right_curb_or_sidewalk_width: 'BG08',
  approach_roadway_width: 'BG09',
  bridge_median: 'BG10',
  skew: 'BG11',
  curved_bridge: 'BG12',
  maximum_bridge_height: 'BG13',
  sidehill_bridge: 'BG14',
  irregular_deck_area: 'BG15',
  calculated_deck_area: 'BG16',
  designed_load: 'BLR01',
  designed_method: 'BLR02',
  load_rating_date: 'BLR03',
  load_rating_method: 'BLR04',
  inventory_load_rating_factor: 'BLR05',
  operating_load_rating_factor: 'BLR06',
  controlling_legal_load_rating_factor: 'BLR07',
  routine_permit_loads: 'BLR08',
  nstm_inspection_required: 'BIR01',
  fatigue_details: 'BIR02',
  underwater_inspection_required: 'BIR03',
  complex_feature: 'BIR04',
  deck_condition_rating: 'BC01',
  superstructure_condition_rating: 'BC02',
  substructure_condition_rating: 'BC03',
  culvert_condition_rating: 'BC04',
  bridge_railings_condition_rating: 'BC05',
  bridge_railings_transitions_condition_rating: 'BC06',
  bridge_bearings_condition_rating: 'BC07',
  bridge_joints_condition_rating: 'BC08',
  channel_condition_rating: 'BC09',
  channel_protection_condition_rating: 'BC10',
  scour_condition_rating: 'BC11',
  bridge_condition_classification: 'BC12',
  lowest_condition_rating_code: 'BC13',
  nstm_inspection_condition: 'BC14',
  underwater_inspection_condition: 'BC15',
  approach_roadway_alignment: 'BAP01',
  overtopping_likelihood: 'BAP02',
  scour_vulnerability: 'BAP03',
  scour_plan_of_action: 'BAP04',
  seismic_vulnerability: 'BAP05',
  year_built: 'BW01',
  element_number: 'BE01',
  element_parent_number: 'BE02',
  element_total_quantity: 'BE03',
  element_quantity_condition_state_one: 'BCS01',
  element_quantity_condition_state_two: 'BCS02',
  element_quantity_condition_state_three: 'BCS03',
  element_quantity_condition_state_four: 'BCS04',
  feature_type: 'BF01',
  feature_location: 'BF02',
  feature_name: 'BF03',
  functional_classification: 'BH01',
  urban_code: 'BH02',
  nhs_designation: 'BH03',
  national_highway_freight_network: 'BH04',
  strahnet_designation: 'BH05',
  lrs_route_id: 'BH06',
  lrs_mile_point: 'BH07',
  lanes_on_highway: 'BH08',
  annual_average_daily_traffic: 'BH09',
  annual_average_daily_truck_traffic: 'BH10',
  year_of_annual_average_daily_traffic: 'BH11',
  highway_max_usable_vertical_clearance: 'BH12',
  highway_min_vertical_clearance: 'BH13',
  highway_min_horizontal_clearance_left: 'BH14',
  highway_min_horizontal_clearance_right: 'BH15',
  highway_max_usable_surface_width: 'BH16',
  bypass_detour_length: 'BH17',
  crossing_bridge_number: 'BH18',
  railroad_service_type: 'BRR01',
  railroad_min_vertical_clearance: 'BRR02',
  railroad_min_horizontal_offset: 'BRR03',
  navigable_waterway: 'BN01',
  navigable_min_vertical_clearance: 'BN02',
  movable_bridge_max_navigation_vertical_clearance: 'BN03',
  navigation_channel_width: 'BN04',
  navigation_channel_min_horizontal_clearance: 'BN05',
  substructure_navigation_protection: 'BN06',
  route_designation: 'BRT01',
  route_number: 'BRT02',
  route_direction: 'BRT03',
  route_type: 'BRT04',
  service_type: 'BRT05',
  inspection_type: 'BIE01',
  inspection_begin_date: 'BIE02',
  inspection_completion_date: 'BIE03',
  nationally_certified_bridge_inspector: 'BIE04',
  inspection_interval: 'BIE05',
  inspection_due_date: 'BIE06',
  risk_based_inspection_interval_method: 'BIE07',
  inspection_quality_control_date: 'BIE08',
  inspection_quality_assurance_date: 'BIE09',
  inspection_data_update_date: 'BIE10',
  inspection_note: 'BIE11',
  inspection_equipment: 'BIE12',
  legal_load_configuration: 'BEP01',
  legal_load_rating_factor: 'BEP02',
  posting_type: 'BEP03',
  posting_value: 'BEP04',
  posting_status_change_date: 'BPS02',
  load_posting_status: 'BPS01',
  span_configuration_designation: 'BSP01',
  number_of_spans: 'BSP02',
  number_of_beam_lines: 'BSP03',
  span_material: 'BSP04',
  span_continuity: 'BSP05',
  span_type: 'BSP06',
  span_protective_system: 'BSP07',
  deck_interaction: 'BSP08',
  deck_material_and_type: 'BSP09',
  wearing_surface: 'BSP10',
  deck_protective_system: 'BSP11',
  deck_reinforcing_protective_system: 'BSP12',
  deck_stay_in_place_forms: 'BSP13',
  substructure_configuration_designation: 'BSB01',
  number_of_substructure_units: 'BSB02',
  substructure_material: 'BSB03',
  substructure_type: 'BSB04',
  substructure_protective_system: 'BSB05',
  foundation_type: 'BSB06',
  foundation_protective_system: 'BSB07',
  year_work_performed: 'BW02',
  work_performed: 'BW03',
};

// Human-readable label for each SNBI code — derived from the column name
const SNBI_FIELD_LABELS: Record<string, string> = Object.fromEntries(
  Object.entries(COLUMN_TO_SNBI_KEY).map(([col, code]) => [
    code,
    col.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
  ])
);

const getFieldLabel = (code: string) =>
  SNBI_FIELD_LABELS[code] ? `${SNBI_FIELD_LABELS[code]} (${code})` : code;

const issueClasses = (issues: ValidationIssue[]) => {
  if (issues.some((issue) => issue.type === 'critical' || issue.type === 'error')) {
    return 'border-red-400 focus:border-red-500 focus:ring-red-200';
  }
  if (issues.length > 0) {
    return 'border-amber-400 focus:border-amber-500 focus:ring-amber-200';
  }
  return 'border-gray-300 dark:border-gray-700';
};

const IssueMessages = ({ issues }: { issues: ValidationIssue[] }) => {
  if (!issues.length) return null;
  return (
    <div className="mt-1 space-y-1">
      {issues.slice(0, 2).map((issue, idx) => (
        <div
          key={`${issue.rule}-${idx}`}
          className={`text-xs rounded-lg px-2 py-1 ${
            issue.type === 'critical' || issue.type === 'error'
              ? 'bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300'
              : 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
          }`}
          title={`${issue.rule}${issue.expected ? ` · Expected: ${issue.expected}` : ''}`}
        >
          <span className="font-semibold uppercase">{issue.type}</span>: {issue.message}
          {issue.expected && <span className="block">Expected: {issue.expected}</span>}
          {issue.editable_hint && <span className="block">{issue.editable_hint}</span>}
        </div>
      ))}
      {issues.length > 2 && (
        <p className="text-xs text-gray-500 dark:text-gray-400">+{issues.length - 2} more validation issue(s)</p>
      )}
    </div>
  );
};

const FieldMetadataMessages = ({ metadata }: { metadata: FieldMetadata[] }) => {
  if (!metadata.length) return null;
  // Status chips (temporary/filled/review) are shown once next to the field label;
  // here we only show the supporting detail (source value, note, transition, rule).
  const details = metadata.filter((item) => item.source_value || item.note || item.rule || item.transition);
  if (!details.length) return null;
  return (
    <div className="mt-1 space-y-1">
      {details.slice(0, 2).map((item, idx) => (
        <div key={`${item.tag}-${idx}`} className="rounded-lg bg-gray-50 dark:bg-gray-900 px-2 py-1 text-xs text-gray-600 dark:text-gray-300">
          {item.source_value && (
            <span className="block">
              <span className="font-semibold">{item.source_name || 'Source'}:</span> {item.source_value}
            </span>
          )}
          {item.note && <span className="block">{item.note}</span>}
          {item.transition && (
            <span className="block text-sky-700 dark:text-sky-300">
              <span className="font-semibold">FHWA transition:</span> {item.transition}
            </span>
          )}
          {item.rule && <span className="block"><span className="font-semibold">Rule:</span> {item.rule}</span>}
        </div>
      ))}
    </div>
  );
};

const TABLE_NAME_TO_SCHEMA_KEY: Record<string, string> = {
  features: 'Features',
  span_sets: 'SpanSets',
  substructure_sets: 'SubstructureSets',
  elements: 'Elements',
  works: 'Works',
  inspections: 'Inspections',
  posting_evaluations: 'PostingEvaluations',
  posting_statuses: 'PostingStatuses',
  routes: 'Routes',
};

const TABLE_ALLOWED_SNBI_KEYS: Record<string, Set<string>> = {
  features: new Set(['BF01', 'BF02', 'BF03', 'BH01', 'BH02', 'BH03', 'BH04', 'BH05', 'BH06', 'BH07', 'BH08', 'BH09', 'BH10', 'BH11', 'BH12', 'BH13', 'BH14', 'BH15', 'BH16', 'BH17', 'BH18', 'BRR01', 'BRR02', 'BRR03', 'BN01', 'BN02', 'BN03', 'BN04', 'BN05', 'BN06']),
  routes: new Set(['BF01', 'BRT01', 'BRT02', 'BRT03', 'BRT04', 'BRT05']),
  span_sets: new Set(['BSP01', 'BSP02', 'BSP03', 'BSP04', 'BSP05', 'BSP06', 'BSP07', 'BSP08', 'BSP09', 'BSP10', 'BSP11', 'BSP12', 'BSP13']),
  substructure_sets: new Set(['BSB01', 'BSB02', 'BSB03', 'BSB04', 'BSB05', 'BSB06', 'BSB07']),
  elements: new Set(['BE01', 'BE02', 'BE03', 'BCS01', 'BCS02', 'BCS03', 'BCS04']),
  inspections: new Set(['BIE01', 'BIE02', 'BIE03', 'BIE04', 'BIE05', 'BIE06', 'BIE07', 'BIE08', 'BIE09', 'BIE10', 'BIE11', 'BIE12']),
  posting_evaluations: new Set(['BEP01', 'BEP02', 'BEP03', 'BEP04']),
  posting_statuses: new Set(['BPS01', 'BPS02']),
  works: new Set(['BW02', 'BW03']),
};

interface StateOption {
  state_code: number;
  state_name: string;
}

const Editor: React.FC<{ initialStateCode?: number; initialBridgeNumber?: string }> = ({ initialStateCode, initialBridgeNumber }) => {
  const [states, setStates] = useState<StateOption[]>([]);

  useEffect(() => {
    axios.get('/api/states')
      .then(res => setStates(res.data))
      .catch(() => setStates([]));
  }, []);

  // When opened for a specific bridge (from the map popup), preselect its state.
  useEffect(() => {
    if (initialStateCode != null) setSelectedStateCode(initialStateCode);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialStateCode]);

  const [selectedStateCode, setSelectedStateCode] = useState<number | null>(null);
  const [bridges, setBridges] = useState<Bridge[]>([]);
  const [selectedBridge, setSelectedBridge] = useState<Bridge | null>(null);
  const [activeTab, setActiveTab] = useState<string>("bridges");

  const [bridgeData, setBridgeData] = useState<any>(null);
  const [childData, setChildData] = useState<Record<string, TableRow[]>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [downloadingAll, setDownloadingAll] = useState(false);
  const [availableYears, setAvailableYears] = useState<number[]>([]);
  const [selectedExportYear, setSelectedExportYear] = useState<number | null>(null);
  const [exportValidation, setExportValidation] = useState<any | null>(null);
  const [bridgeValidation, setBridgeValidation] = useState<any | null>(null);
  const [fieldMetadata, setFieldMetadata] = useState<any | null>(null);
  const [message, setMessage] = useState<{ type: 'success' | 'error' | 'info'; text: string } | null>(null);

  const sanitizeFileSegment = (value: string) => {
    return value.replace(/[^a-zA-Z0-9_-]/g, '_');
  };

  const buildBaseFileName = () => {
    const stateCode = selectedBridge?.state_code ?? selectedStateCode ?? null;
    const stateOption = states.find((s) => s.state_code === stateCode);
    const stateName = stateOption ? stateOption.state_name : String(stateCode ?? 'state');
    const bridge = selectedBridge?.bridge_number ?? 'bridge';
    return `${sanitizeFileSegment(stateName)}_${sanitizeFileSegment(String(bridge))}`;
  };

  const currentBridgeValidation = () => {
    if (!bridgeValidation || !selectedBridge) return null;
    return bridgeValidation.by_bridge?.[String(selectedBridge.bridge_number)] || null;
  };

  const getMainIssues = (field: string): ValidationIssue[] => {
    const tag = COLUMN_TO_SNBI_KEY[field] || field;
    return currentBridgeValidation()?.by_tag?.[tag] || [];
  };

  const getMainMetadata = (field: string): FieldMetadata[] => {
    const tag = COLUMN_TO_SNBI_KEY[field] || field;
    return fieldMetadata?.by_tag?.[tag] || [];
  };

  const getChildIssues = (table: string, rowIndex: number, field: string): ValidationIssue[] => {
    const tag = COLUMN_TO_SNBI_KEY[field] || field;
    return currentBridgeValidation()?.by_table?.[table]?.[String(rowIndex)]?.[tag] || [];
  };

  const getChildMetadata = (table: string, rowIndex: number, field: string): FieldMetadata[] => {
    const tag = COLUMN_TO_SNBI_KEY[field] || field;
    return fieldMetadata?.by_table?.[table]?.[String(rowIndex)]?.[tag] || [];
  };

  const getTableIssues = (table: string): ValidationIssue[] => {
    const tableRows = currentBridgeValidation()?.by_table?.[table] || {};
    return Object.values(tableRows).flatMap((row: any) =>
      Object.values(row || {}).flatMap((issues: any) => Array.isArray(issues) ? issues : [])
    ) as ValidationIssue[];
  };

  const validationCountForTab = (tab: string) => {
    const current = currentBridgeValidation();
    if (!current) return 0;
    if (tab === 'bridges') {
      return Object.values(current.by_tag || {}).reduce((sum: number, list: any) => sum + (Array.isArray(list) ? list.length : 0), 0);
    }
    const tableRows = current.by_table?.[tab] || {};
    return Object.values(tableRows).reduce((sum: number, row: any) => {
      return sum + Object.values(row || {}).reduce((rowSum: number, list: any) => rowSum + (Array.isArray(list) ? list.length : 0), 0);
    }, 0);
  };

  const fetchBridgeValidation = async (bridge: Bridge) => {
    const yearQuery = selectedExportYear ? `?year=${selectedExportYear}` : '';
    try {
      const res = await axios.get(`/api/validate-bridge/${bridge.state_code}/${encodeURIComponent(bridge.bridge_number)}${yearQuery}`);
      setBridgeValidation(res.data);
    } catch (err) {
      console.error('Failed to load bridge validation:', err);
      setBridgeValidation(null);
    }
  };

  const fetchFieldMetadata = async (bridge: Bridge) => {
    const yearQuery = selectedExportYear ? `?year=${selectedExportYear}` : '';
    try {
      const res = await axios.get(`/api/bridge-field-metadata/${bridge.state_code}/${encodeURIComponent(bridge.bridge_number)}${yearQuery}`);
      setFieldMetadata(res.data);
    } catch (err) {
      console.error('Failed to load bridge field metadata:', err);
      setFieldMetadata(null);
    }
  };

  const escapeCsvValue = (value: unknown) => {
    if (value === null || value === undefined) {
      return '';
    }
    const text = String(value).replace(/\r?\n|\r/g, ' ');
    if (/[",]/.test(text)) {
      return `"${text.replace(/"/g, '""')}"`;
    }
    return text;
  };

  const tableToCsv = (rows: TableRow[]) => {
    const columns = Array.from(new Set(rows.flatMap((row) => Object.keys(row))));
    if (columns.length === 0) {
      return '';
    }

    const header = columns.join(',');
    const body = rows.map((row) => columns.map((column) => escapeCsvValue(row[column])).join(','));
    return [header, ...body].join('\n');
  };

  const triggerFileDownload = (blob: Blob, fileName: string) => {
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
  };

  const cleanValue = (value: any): any => {
    if (typeof value !== 'string') return value;
    const trimmed = value.trim().replace(/^['"]+|['"]+$/g, '').trim();
    return trimmed;
  };

  const mapRowToSnbi = (row: Record<string, any>, tableName?: string) => {
    const out: Record<string, any> = {};
    const allowed = tableName ? TABLE_ALLOWED_SNBI_KEYS[tableName] : null;
    Object.entries(row || {}).forEach(([key, value]) => {
      const snbiKey = COLUMN_TO_SNBI_KEY[key];
      if (!snbiKey) return;
      if (allowed && !allowed.has(snbiKey)) return;
      const cleaned = cleanValue(value);
      if (cleaned === null || cleaned === undefined || cleaned === '') return;
      out[snbiKey] = cleaned;
    });
    return out;
  };

  const cleanSnbiExport = (value: any): any => {
    if (Array.isArray(value)) {
      return value
        .map((item) => cleanSnbiExport(item))
        .filter((item) => item !== null && item !== undefined && !(typeof item === 'object' && Object.keys(item).length === 0));
    }
    if (!value || typeof value !== 'object') {
      return cleanValue(value);
    }
    const out: Record<string, any> = {};
    Object.entries(value).forEach(([key, val]) => {
      if (INTERNAL_EXPORT_KEYS.has(key)) return;
      const cleaned = cleanSnbiExport(val);
      if (cleaned === null || cleaned === undefined || cleaned === '') return;
      out[key] = cleaned;
    });
    return out;
  };

  const normalizeBridgeForFhwa = (bridge: Record<string, any>) => {
    const toNumber = (value: any) => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    };
    const postingEvaluations = Array.isArray(bridge.PostingEvaluations) ? bridge.PostingEvaluations : [];
    const legalLoadFactors = postingEvaluations
      .map((row: Record<string, any>) => toNumber(row.BEP02))
      .filter((value: number | null): value is number => value !== null);
    if (legalLoadFactors.length > 0) {
      bridge.BLR07 = Math.trunc(Math.min(...legalLoadFactors) * 100) / 100;
    } else if ((bridge.BLR07 === null || bridge.BLR07 === undefined || bridge.BLR07 === '') && bridge.BLR06) {
      bridge.BLR07 = bridge.BLR06;
    }

    if (!bridge.BLR08) bridge.BLR08 = 'N';
    if (!bridge.BG12) bridge.BG12 = 'N';
    if (!bridge.BAP05 || bridge.BAP05 === 'U') bridge.BAP05 = '0';
    if (!bridge.BG13 || bridge.BG13 === '0' || bridge.BG13 === 0) bridge.BG13 = 1;
    if (!bridge.BG14) bridge.BG14 = 'N';

    const features = Array.isArray(bridge.Features) ? bridge.Features : [];
    const hasWaterway = features.some((feature: Record<string, any>) => String(feature.BF01 || '').startsWith('W'));
    if (hasWaterway && String(bridge.BC09 || '').toUpperCase() === 'N') {
      bridge.BC09 = '0';
    }
    features.forEach((feature: Record<string, any>) => {
      const featureType = String(feature.BF01 || '');
      if (featureType.startsWith('W') && !feature.BN01) {
        feature.BN01 = 'N';
      }
      if (!featureType.startsWith('W')) {
        ['BN01', 'BN02', 'BN03', 'BN04', 'BN05', 'BN06'].forEach((tag) => delete feature[tag]);
      }
      if (feature.BN01 === 'N' || feature.BN01 === 'U') {
        ['BN02', 'BN03', 'BN04', 'BN05', 'BN06'].forEach((tag) => delete feature[tag]);
      }
      if (featureType.startsWith('H') && Array.isArray(feature.Routes)) {
        feature.Routes.forEach((route: Record<string, any>) => {
          if (!route.BRT03) route.BRT03 = '2-T';
        });
      }
    });

    const substructures = Array.isArray(bridge.SubstructureSets) ? bridge.SubstructureSets : [];
    substructures.forEach((sub: Record<string, any>, idx: number) => {
      if (!sub.BSB01 || String(sub.BSB01).startsWith('SS')) sub.BSB01 = idx === 0 ? 'P01' : `P${String(idx + 1).padStart(2, '0')}`;
      if (!sub.BSB02 || sub.BSB02 === '0' || sub.BSB02 === 0) sub.BSB02 = 1;
      if (!sub.BSB03 || sub.BSB03 === 'X-T') sub.BSB03 = 'C01';
      if (!sub.BSB04) sub.BSB04 = String(sub.BSB01 || '').startsWith('P') ? 'P01' : 'A01';
      if (!sub.BSB05) sub.BSB05 = '0';
      if (!sub.BSB06) sub.BSB06 = 'X';
      if (!sub.BSB07) sub.BSB07 = '0';
    });

    const spans = Array.isArray(bridge.SpanSets) ? bridge.SpanSets : [];
    spans.forEach((span: Record<string, any>) => {
      if (!span.BSP03 || span.BSP03 === '0' || span.BSP03 === 0) span.BSP03 = 1;
      if (!span.BSP07) span.BSP07 = '0';
      if (String(span.BSP09 || '') === '0') {
        delete span.BSP08;
        delete span.BSP13;
      } else {
        if (!span.BSP08) span.BSP08 = '0';
        if (!span.BSP13) span.BSP13 = '0';
      }
    });

    const hasSteel = spans.some((span: Record<string, any>) => String(span.BSP04 || '').startsWith('S')) ||
      substructures.some((sub: Record<string, any>) => String(sub.BSB03 || '').startsWith('S'));
    if (!hasSteel) delete bridge.BIR02;
    if (bridge.BIR01 !== 'Y') delete bridge.BC14;
    else if (!bridge.BC14) bridge.BC14 = 'N';
    if (bridge.BIR03 === 'Y' && !bridge.BC15) bridge.BC15 = 'N';
    if (bridge.BIR03 !== 'Y') delete bridge.BC15;
    const works = Array.isArray(bridge.Works) ? bridge.Works : [];
    works.forEach((work: Record<string, any>) => {
      if (!work.BW03) work.BW03 = '0';
    });
    if (!bridge.BG05 && (bridge.BG06 || bridge.BG09)) bridge.BG05 = bridge.BG06 || bridge.BG09;
    return bridge;
  };

  const buildSnbiBridgePayload = () => {
    const mainBridge = {
      ...mapRowToSnbi({
      ...(bridgeData || {}),
      state_code: selectedBridge?.state_code ?? selectedStateCode,
      bridge_number: selectedBridge?.bridge_number,
      }),
      // Keep existing SNBI-coded keys when Editor has /api/bridge payload directly.
      ...(bridgeData || {}),
      BL01: selectedBridge?.state_code ?? selectedStateCode,
      BID01: selectedBridge?.bridge_number,
    };

    CHILD_TABLES.filter((tableName) => tableName !== 'routes').forEach((tableName) => {
      const schemaKey = TABLE_NAME_TO_SCHEMA_KEY[tableName];
      if (!schemaKey) return;
      const mappedRows = (childData[tableName] || []).map((row) => mapRowToSnbi(row, tableName));
      mainBridge[schemaKey] = mappedRows;
    });

    // Routes are nested under Features in SNBI schema. Attach by feature type (BF01).
    const routeRows = (childData.routes || []).map((row) => mapRowToSnbi(row, 'routes'));
    const routesByFeature = new Map<string, TableRow[]>();
    routeRows.forEach((route) => {
      const featureType = String(route.BF01 || '').trim();
      if (!featureType) return;
      const routeOnly = { ...route };
      delete routeOnly.BF01;
      if (!routesByFeature.has(featureType)) {
        routesByFeature.set(featureType, []);
      }
      routesByFeature.get(featureType)?.push(routeOnly);
    });

    const features = Array.isArray(mainBridge.Features) ? mainBridge.Features : [];
    features.forEach((feature: TableRow) => {
      const featureType = String(feature.BF01 || '').trim();
      if (!featureType) return;
      const routes = routesByFeature.get(featureType) || [];
      if (routes.length > 0) {
        feature.Routes = routes;
      }
      routesByFeature.delete(featureType);
    });

    // Keep orphan routes by creating minimal feature shells so data is not lost.
    routesByFeature.forEach((routes, featureType) => {
      features.push({ BF01: featureType, Routes: routes });
    });
    mainBridge.Features = features;

    return [cleanSnbiExport(normalizeBridgeForFhwa(mainBridge))];
  };

  const downloadBridgeJson = () => {
    if (!bridgeData) {
      setMessage({ type: 'error', text: 'No bridge data available to download.' });
      return;
    }

    const baseName = buildBaseFileName();
    const payload = buildSnbiBridgePayload();

    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8;' });
    triggerFileDownload(blob, `${baseName}_bridge.json`);
    setMessage({ type: 'success', text: 'Downloaded bridge JSON file (SNBI schema format).' });
  };

  // Load bridges when state changes
  useEffect(() => {
    if (!selectedStateCode) return;

    const fetchBridges = async () => {
      setLoading(true);
      try {
        const response = await axios.get(`/api/table/bridges?state_code=${selectedStateCode}`);
        setBridges(response.data);
      } catch (err) {
        console.error(err);
        setMessage({ type: 'error', text: 'Failed to load bridges' });
      } finally {
        setLoading(false);
      }
    };

    fetchBridges();
  }, [selectedStateCode]);

  // Load available years when state changes
  useEffect(() => {
    if (!selectedStateCode) {
      setAvailableYears([]);
      setSelectedExportYear(null);
      return;
    }
    axios.get<{ years: number[] }>(`/api/available-years/${selectedStateCode}`)
      .then((res) => {
        setAvailableYears(res.data.years);
        setSelectedExportYear(res.data.years[0] ?? null);
      })
      .catch(() => {
        setAvailableYears([]);
        setSelectedExportYear(null);
      });
  }, [selectedStateCode]);

  // Load full bridge data when a bridge is selected
  const loadBridgeDetails = async (bridge: Bridge) => {
    setSelectedBridge(bridge);
    setLoading(true);
    setBridgeData(null);
    setChildData({});
    setBridgeValidation(null);
    setFieldMetadata(null);

    try {
      console.log(`Loading bridge details for ${bridge.state_code}/${bridge.bridge_number}`);
      const response = await axios.get(`/api/bridge/${bridge.state_code}/${bridge.bridge_number}`);
      console.log('Bridge data received:', response.data);
      setBridgeData(response.data);

      // Load child tables
      const childPromises = CHILD_TABLES.map(async (table) => {
        try {
          console.log(`Loading ${table} for bridge ${bridge.bridge_number}`);
          const res = await axios.get(`/api/table/${table}?state_code=${bridge.state_code}&bridge_number=${bridge.bridge_number}&limit=100`);
          console.log(`${table} data:`, res.data);
          return { table, data: res.data };
        } catch (error) {
          console.error(`Failed to load ${table}:`, error);
          return { table, data: [] };
        }
      });

      const results = await Promise.all(childPromises);
      const newChildData: Record<string, TableRow[]> = {};
      results.forEach(({ table, data }) => {
        newChildData[table] = data;
      });
      setChildData(newChildData);
      await fetchBridgeValidation(bridge);
      await fetchFieldMetadata(bridge);

    } catch (err: any) {
      console.error('Failed to load bridge details:', err);
      setMessage({
        type: 'error',
        text: `Failed to load bridge details: ${err.response?.data?.detail || err.message}`
      });
      setBridgeData(null);
      setChildData({});
      setFieldMetadata(null);
    } finally {
      setLoading(false);
    }
  };

  // Once the requested state's bridges load, auto-open the target bridge directly.
  useEffect(() => {
    if (!initialBridgeNumber || selectedBridge || bridges.length === 0) return;
    const match = bridges.find((b) => String(b.bridge_number) === String(initialBridgeNumber));
    if (match) loadBridgeDetails(match);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bridges, initialBridgeNumber]);

  const handleSaveMainBridge = async () => {
    if (!selectedBridge || !bridgeData) return;

    setSaving(true);
    setMessage({ type: 'info', text: 'Saving bridge changes to database...' });
    try {
      const response = await axios.put(
        `/api/bridge/${selectedBridge.state_code}/${selectedBridge.bridge_number}`,
        bridgeData
      );
      const saved = response.data?.data || bridgeData;
      setBridgeData(saved);
      await fetchBridgeValidation(selectedBridge);
      await fetchFieldMetadata(selectedBridge);
      setMessage({ type: 'success', text: 'Main bridge data saved successfully!' });
    } catch (err: any) {
      setMessage({ type: 'error', text: err.response?.data?.detail || 'Failed to save changes' });
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => {
    if (selectedBridge) {
      fetchBridgeValidation(selectedBridge);
      fetchFieldMetadata(selectedBridge);
    }
  }, [selectedExportYear]);

  const handleChildTableChange = (table: string, index: number, field: string, value: any) => {
    const updated = [...(childData[table] || [])];
    updated[index] = { ...updated[index], [field]: value };
    setChildData(prev => ({ ...prev, [table]: updated }));
  };

  // Append a blank row to the active child table (templated from an existing row, or
  // the table schema when empty), pre-scoped to this bridge. The new row flows into
  // the bridge JSON download.
  const addChildRow = async () => {
    const rows = childData[activeTab] || [];
    const template: Record<string, any> = {};
    if (rows.length > 0) {
      Object.keys(rows[0]).forEach((k) => { template[k] = ''; });
    } else {
      try {
        const res = await axios.get(`/api/table-schema/${activeTab}`);
        const colsMeta: any[] = res.data?.columns || res.data || [];
        colsMeta.forEach((c: any) => { const n = typeof c === 'string' ? c : c?.name; if (n && n !== 'id' && n !== 'rowid') template[n] = ''; });
      } catch { /* fall back to an empty row */ }
    }
    if ('state_code' in template) template.state_code = selectedBridge?.state_code ?? selectedStateCode;
    if ('bridge_number' in template) template.bridge_number = selectedBridge?.bridge_number;
    if ('data_year' in template && selectedExportYear) template.data_year = selectedExportYear;
    setChildData((prev) => ({ ...prev, [activeTab]: [...(prev[activeTab] || []), template] }));
  };

  const downloadTable = (table: string) => {
    const rows = childData[table] || [];
    const csvContent = tableToCsv(rows);
    const baseName = buildBaseFileName();
    const fileName = `${baseName}_${sanitizeFileSegment(table)}.csv`;
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    triggerFileDownload(blob, fileName);
    setMessage({ type: 'success', text: `Downloaded ${table} table as CSV.` });
  };

  const downloadAllBridgesZip = async () => {
    if (!selectedStateCode) {
      setMessage({ type: 'error', text: 'Please select a state first.' });
      return;
    }
    try {
      setDownloadingAll(true);
      const stateOption = states.find((s) => s.state_code === selectedStateCode);
      const stateName = stateOption ? sanitizeFileSegment(stateOption.state_name) : String(selectedStateCode);

      const yearQuery = selectedExportYear ? `?year=${selectedExportYear}` : '';
      const validationResponse = await axios.get(`/api/validate-bridges/${selectedStateCode}${yearQuery}`);
      setExportValidation(validationResponse.data);
      const response = await axios.get(`/api/export-bridges/${selectedStateCode}${yearQuery}`);
      const allBridges: any[] = Array.isArray(response.data) ? response.data : [];

      if (allBridges.length === 0) {
        setMessage({ type: 'error', text: 'No bridges found for this state.' });
        return;
      }

      const cleanBridge = (bridge: any): any => {
        return cleanSnbiExport(bridge);
      };

      const zip = new JSZip();
      for (const bridge of allBridges) {
        const cleaned = cleanBridge(bridge);
        const bridgeNum = sanitizeFileSegment(String(cleaned.BID01 || bridge.BID01 || 'unknown'));
        // Wrap in array to match single-bridge download format
        zip.file(`${bridgeNum}.json`, JSON.stringify([cleaned], null, 2));
      }

      const zipBlob = await zip.generateAsync({ type: 'blob' });
      const yearSuffix = selectedExportYear ? `_${selectedExportYear}` : '';
      triggerFileDownload(zipBlob, `${stateName}${yearSuffix}_all_bridges.zip`);
      setMessage({ type: 'success', text: `Downloaded ${allBridges.length} bridges as individual JSON files in a ZIP.` });
    } catch (err: any) {
      console.error('Failed to generate ZIP:', err);
      setMessage({ type: 'error', text: err.response?.data?.detail || 'Failed to create ZIP file.' });
    } finally {
      setDownloadingAll(false);
    }
  };

  return (
    <div className="space-y-8">
      <SectionHeader
        icon={<Edit3 className="w-6 h-6" />}
        title="BridgeGuard Editor"
        subtitle="Select a state and bridge to view and edit detailed records"
      />

      {/* State & Bridge Selection */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="glass rounded-3xl p-8 shadow-sm">
          <label className="block text-sm font-medium mb-3 text-gray-700 dark:text-gray-300">Select State</label>
          <select
            value={selectedStateCode || ''}
            onChange={(e) => setSelectedStateCode(Number(e.target.value))}
            className="w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded-2xl px-5 py-4 focus:outline-none focus:ring-2 focus:ring-violet-500 text-gray-900 dark:text-white"
          >
            <option value="">— Choose State —</option>
            {states.map(s => (
              <option key={s.state_code} value={s.state_code}>{s.state_name}</option>
            ))}
          </select>
        </div>

        <div className="glass rounded-3xl p-8 shadow-sm">
          <label className="block text-sm font-medium mb-3 text-gray-700 dark:text-gray-300">
            Select Bridge {bridges.length > 0 && `(${bridges.length} available)`}
          </label>
          <select
            value={selectedBridge?.bridge_number || ''}
            onChange={(e) => {
              const bridge = bridges.find(b => b.bridge_number === e.target.value);
              if (bridge) loadBridgeDetails(bridge);
            }}
            disabled={!selectedStateCode || bridges.length === 0 || loading}
            className="w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded-2xl px-5 py-4 focus:outline-none focus:ring-2 focus:ring-violet-500 disabled:opacity-50 text-gray-900 dark:text-white"
          >
            <option value="">
              {loading ? "Loading bridges..." : bridges.length === 0 ? "No bridges found" : "— Select Bridge —"}
            </option>
            {bridges.map(b => (
              <option
                key={b.bridge_number}
                value={b.bridge_number}
                className={b.fhwa_omit ? 'text-amber-600 dark:text-amber-400' : undefined}
              >
                {b.fhwa_omit ? '⚠ ' : ''}{b.bridge_number} — {b.bridge_name || b.structure_number_008 || 'No Name'}{b.fhwa_omit ? ' — non-highway (FHWA: omit)' : ''}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Download All — shown whenever a state is selected */}
      {selectedStateCode && (
        <div className="glass rounded-3xl p-6 shadow-sm">
          <div className="flex flex-col md:flex-row md:items-end gap-4">
            <div className="flex-1">
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Inspection Year
              </label>
              {availableYears.length > 0 ? (
                <select
                  value={selectedExportYear ?? ''}
                  onChange={(e) => setSelectedExportYear(Number(e.target.value))}
                  className="w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded-2xl px-4 py-3 text-sm text-gray-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-violet-500"
                >
                  {availableYears.map((y) => (
                    <option key={y} value={y}>{y}</option>
                  ))}
                </select>
              ) : (
                <div className="bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-2xl px-4 py-3 text-sm text-gray-400">
                  No year history found for this state
                </div>
              )}
              <p className="mt-2 text-xs text-gray-400 dark:text-gray-500">
                The selected year controls which yearly SNBI snapshot is exported. Internal database fields are not included in the JSON files.
              </p>
              {exportValidation?.summary && (
                <div className="mt-3 rounded-xl border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/30 p-3">
                  <p className="text-sm font-medium text-amber-800 dark:text-amber-300">SNBI validation warnings for export</p>
                  <p className="text-xs text-amber-700 dark:text-amber-200 mt-1">
                    Critical: {exportValidation.summary.critical} · Errors: {exportValidation.summary.error} ·
                    Flags: {exportValidation.summary.flag} · Safety: {exportValidation.summary.safety}
                  </p>
                </div>
              )}
            </div>
            <div className="shrink-0">
              <button
                onClick={downloadAllBridgesZip}
                disabled={downloadingAll || bridges.length === 0}
                className="flex items-center gap-2 text-sm text-white bg-violet-600 hover:bg-violet-700 px-5 py-3 rounded-2xl shadow-sm disabled:opacity-50 whitespace-nowrap"
              >
                {downloadingAll ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                {downloadingAll
                  ? 'Preparing ZIP...'
                  : `Download All Bridges (ZIP)${bridges.length > 0 ? ` — ${bridges.length}` : ''}`}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Editor Content */}
      {selectedBridge ? (
        <div className="glass rounded-3xl shadow-sm border border-gray-200 dark:border-gray-700 relative">
          {loading ? (
            <div className="p-16 text-center">
              <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-violet-600 mx-auto mb-4"></div>
              <p className="text-gray-600 dark:text-gray-400">Loading bridge details...</p>
            </div>
          ) : (
            <>
              {/* Bridge-level FHWA transition notes (e.g. non-highway bridge) */}
              {((fieldMetadata?.bridge_notes?.length ?? 0) > 0 || selectedBridge?.fhwa_omit) && (
                <div className="mx-8 mt-6 space-y-2">
                  {(fieldMetadata?.bridge_notes?.length
                    ? fieldMetadata.bridge_notes
                    : [{ note: 'Legacy Item 42A marks this as a non-highway bridge. The FHWA SNBI crosswalk states such records should be omitted from the NBI transition. Kept for completeness — review before including it in an FHWA submission.' }]
                  ).map((n: any, i: number) => (
                    <div key={i} className="rounded-2xl border border-orange-300 dark:border-orange-800 bg-orange-50 dark:bg-orange-900/30 px-4 py-3">
                      <p className="text-sm font-semibold text-orange-800 dark:text-orange-300">⚠ Non-highway bridge — FHWA transition note</p>
                      <p className="text-xs text-orange-700 dark:text-orange-200 mt-1">{n.note}</p>
                    </div>
                  ))}
                </div>
              )}
              {/* Tabs */}
              <div className="flex border-b border-gray-200 dark:border-gray-700 px-8 pt-6">
                <button
                  onClick={() => setActiveTab("bridges")}
                  className={`px-8 py-4 font-medium border-b-2 transition-colors ${activeTab === "bridges" ? "border-violet-600 text-violet-600 dark:text-violet-400" : "border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"}`}
                >
                  Main Bridge Info
                  {validationCountForTab('bridges') > 0 && (
                    <span className="ml-2 rounded-full bg-red-100 px-2 py-0.5 text-xs text-red-700 dark:bg-red-900/40 dark:text-red-300">
                      {validationCountForTab('bridges')}
                    </span>
                  )}
                </button>
                {CHILD_TABLES.map(table => (
                  <button
                    key={table}
                    onClick={() => setActiveTab(table)}
                    className={`px-8 py-4 font-medium border-b-2 transition-colors ${activeTab === table ? "border-violet-600 text-violet-600 dark:text-violet-400" : "border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"}`}
                  >
                    {table.replace('_', ' ').toUpperCase()}
                    {validationCountForTab(table) > 0 && (
                      <span className="ml-2 rounded-full bg-red-100 px-2 py-0.5 text-xs text-red-700 dark:bg-red-900/40 dark:text-red-300">
                        {validationCountForTab(table)}
                      </span>
                    )}
                  </button>
                ))}
              </div>

          {/* Main Bridge Tab */}
          {activeTab === "bridges" && bridgeData && (
            <div className="p-8">
              <div className="sticky top-0 z-20 -mx-8 px-8 py-3 mb-6 flex items-center justify-between gap-3 border-b border-gray-200 dark:border-gray-700 bg-white/95 dark:bg-gray-800/95 backdrop-blur">
                <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-100">Main Bridge Info</h3>
                <div className="flex items-center gap-2">
                  <button
                    onClick={downloadBridgeJson}
                    className="flex items-center gap-2 text-sm text-violet-600 hover:bg-violet-50 dark:hover:bg-violet-900/20 px-3 py-2 rounded-xl border border-violet-200 dark:border-violet-800"
                  >
                    <Download className="w-4 h-4" /> Download JSON
                  </button>
                  <button
                    onClick={handleSaveMainBridge}
                    disabled={saving}
                    className="flex items-center gap-2 bg-violet-600 hover:bg-violet-700 text-white px-5 py-2 rounded-xl font-semibold shadow-sm disabled:opacity-70 transition-all"
                  >
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    {saving ? "Saving…" : "Save"}
                  </button>
                </div>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {SNBI_MAIN_FIELDS.map(key => {
                  const issues = getMainIssues(key);
                  const metadata = getMainMetadata(key);
                  return (
                  <div key={key} className="space-y-1">
                    <label className="flex items-center gap-2 text-xs font-medium text-gray-700 dark:text-gray-300">
                      {getFieldLabel(key)}
                      {issues.length > 0 && (
                        <span className={`rounded-full px-2 py-0.5 text-[10px] uppercase ${
                          issues.some((issue) => issue.type === 'critical' || issue.type === 'error')
                            ? 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'
                            : 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
                        }`}>
                          {issues[0].type}
                        </span>
                      )}
                      {metadata.some((item) => item.is_temporary) && (
                        <span className="rounded-full bg-purple-100 px-2 py-0.5 text-[10px] uppercase text-purple-700 dark:bg-purple-900/40 dark:text-purple-300">
                          temp
                        </span>
                      )}
                      {metadata.some((item) => item.is_default) && (
                        <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] uppercase text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                          filled
                        </span>
                      )}
                      {metadata.some((item) => item.requires_review) && (
                        <span className="rounded-full bg-orange-100 px-2 py-0.5 text-[10px] uppercase text-orange-700 dark:bg-orange-900/40 dark:text-orange-300">
                          review
                        </span>
                      )}
                    </label>
                    <input
                      type="text"
                      value={bridgeData[key] ?? ''}
                      onChange={(e) => setBridgeData({ ...bridgeData, [key]: e.target.value })}
                      title={issues[0]?.expected || metadata[0]?.source_value || metadata[0]?.note || undefined}
                      className={`w-full bg-gray-50 dark:bg-gray-900 border rounded-xl px-4 py-3 text-sm text-gray-900 dark:text-white focus:outline-none focus:ring-2 ${issueClasses(issues)}`}
                    />
                    <IssueMessages issues={issues} />
                    <FieldMetadataMessages metadata={metadata} />
                  </div>
                  );
                })}
              </div>

            </div>
          )}

          {/* Child Table Tabs */}
          {CHILD_TABLES.includes(activeTab) && (
            <div className="p-8">
              <div className="sticky top-0 z-20 -mx-8 px-8 py-3 mb-6 flex items-center justify-between gap-3 border-b border-gray-200 dark:border-gray-700 bg-white/95 dark:bg-gray-800/95 backdrop-blur">
                <h3 className="text-lg font-semibold capitalize text-gray-800 dark:text-gray-100">
                  {activeTab.replace('_', ' ')}
                  {childData[activeTab]?.length ? <span className="ml-2 text-sm font-normal text-gray-400">({childData[activeTab].length})</span> : null}
                </h3>
                <div className="flex items-center gap-2">
                  <button
                    onClick={downloadBridgeJson}
                    className="flex items-center gap-2 text-sm text-violet-600 hover:bg-violet-50 dark:hover:bg-violet-900/20 px-3 py-2 rounded-xl border border-violet-200 dark:border-violet-800"
                  >
                    <Download className="w-4 h-4" /> Download JSON
                  </button>
                  <button
                    onClick={() => downloadTable(activeTab)}
                    className="flex items-center gap-2 text-sm text-violet-600 hover:bg-violet-50 dark:hover:bg-violet-900/20 px-3 py-2 rounded-xl border border-violet-200 dark:border-violet-800"
                  >
                    <Download className="w-4 h-4" /> Download Table
                  </button>
                  <button
                    onClick={addChildRow}
                    className="flex items-center gap-2 bg-violet-600 hover:bg-violet-700 text-white px-4 py-2 rounded-xl font-semibold shadow-sm"
                  >
                    <Plus className="w-4 h-4" /> Add Row
                  </button>
                </div>
              </div>

              {childData[activeTab] && childData[activeTab].length > 0 ? (
                <div className="overflow-x-auto">
                  {getTableIssues(activeTab).length > 0 && (
                    <div className="mb-4 rounded-xl border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/30 p-3">
                      <p className="text-sm font-medium text-amber-800 dark:text-amber-300">Validation issues in this table</p>
                      <div className="mt-2 space-y-1">
                        {getTableIssues(activeTab).slice(0, 5).map((issue, idx) => (
                          <p key={`${issue.rule}-${idx}`} className="text-xs text-amber-700 dark:text-amber-200">
                            <span className="font-semibold uppercase">{issue.type}</span> {issue.tag}: {issue.message}
                          </p>
                        ))}
                      </div>
                    </div>
                  )}
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 dark:bg-gray-900">
                        {Object.keys(childData[activeTab][0] || {}).map(col => (
                          <th key={col} className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">{getFieldLabel(col)}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {childData[activeTab].map((row, idx) => (
                        <tr key={idx} className="border-t border-gray-100 dark:border-gray-800">
                          {Object.keys(row).map((key, i) => {
                            const issues = getChildIssues(activeTab, idx, key);
                            const metadata = getChildMetadata(activeTab, idx, key);
                            return (
                            <td key={i} className="px-4 py-3 align-top">
                              <div className="mb-1 flex flex-wrap gap-1">
                                {metadata.some((item) => item.is_temporary) && (
                                  <span className="rounded-full bg-purple-100 px-2 py-0.5 text-[10px] uppercase text-purple-700 dark:bg-purple-900/40 dark:text-purple-300">
                                    temp
                                  </span>
                                )}
                                {metadata.some((item) => item.is_default) && (
                                  <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] uppercase text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                                    filled
                                  </span>
                                )}
                                {metadata.some((item) => item.requires_review) && (
                                  <span className="rounded-full bg-orange-100 px-2 py-0.5 text-[10px] uppercase text-orange-700 dark:bg-orange-900/40 dark:text-orange-300">
                                    review
                                  </span>
                                )}
                              </div>
                              <input
                                type="text"
                                value={row[key] ?? ''}
                                onChange={(e) => handleChildTableChange(activeTab, idx, key, e.target.value)}
                                title={issues[0]?.expected || metadata[0]?.source_value || metadata[0]?.note || undefined}
                                className={`w-full bg-transparent border rounded px-2 py-1 text-sm text-gray-900 dark:text-white focus:outline-none focus:ring-2 ${issues.length ? issueClasses(issues) : 'border-transparent focus:border-gray-300 dark:focus:border-gray-600'}`}
                              />
                              <IssueMessages issues={issues} />
                              <FieldMetadataMessages metadata={metadata} />
                            </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="py-12 text-center">
                  <p className="text-gray-500">No records found in this table.</p>
                  {getTableIssues(activeTab).length > 0 && (
                    <div className="mt-4 mx-auto max-w-2xl rounded-xl border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/30 p-4 text-left">
                      <p className="text-sm font-medium text-red-800 dark:text-red-300">Missing or invalid dataset</p>
                      {getTableIssues(activeTab).slice(0, 6).map((issue, idx) => (
                        <p key={`${issue.rule}-${idx}`} className="mt-2 text-xs text-red-700 dark:text-red-200">
                          <span className="font-semibold uppercase">{issue.type}</span> {issue.tag}: {issue.message}
                          {issue.expected && <span className="block">Expected: {issue.expected}</span>}
                        </p>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
            </>
          )}
        </div>
      ) : selectedStateCode ? (
        <div className="glass rounded-3xl shadow-sm border border-gray-200 dark:border-gray-700 p-16 text-center">
          <Edit3 className="w-12 h-12 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
          <p className="text-gray-600 dark:text-gray-400 text-lg font-medium mb-2">Select a Bridge</p>
          <p className="text-gray-500 dark:text-gray-500">Choose a bridge from the dropdown above to view and edit its details</p>
        </div>
      ) : (
        <div className="glass rounded-3xl shadow-sm border border-gray-200 dark:border-gray-700 p-16 text-center">
          <Edit3 className="w-12 h-12 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
          <p className="text-gray-600 dark:text-gray-400 text-lg font-medium mb-2">Select a State First</p>
          <p className="text-gray-500 dark:text-gray-500">Choose a state from the dropdown above to load available bridges</p>
        </div>
      )}

      {/* Status Message */}
      {message && (
        <div className={`p-5 rounded-2xl ${
          message.type === 'success'
            ? 'bg-emerald-50 text-emerald-700'
            : message.type === 'info'
              ? 'bg-blue-50 text-blue-700'
              : 'bg-red-50 text-red-700'
        }`}>
          {message.text}
        </div>
      )}
    </div>
  );
};

export default Editor;