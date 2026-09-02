// frontend/src/pages/Editor.tsx

import React, { useState, useEffect, useRef } from 'react';
import axios, { type AxiosResponse } from 'axios';
import { Edit3, Save, Plus, Loader2, Download, Trash2 } from 'lucide-react';
import { SectionHeader } from '../components/ui';

interface Bridge {
  state_code: number;
  bridge_number: string;
  bridge_name?: string;
  revision_id: string;
  data_year: number;
  revision_number: number;
  content_hash: string;
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

const isPlainObject = (value: unknown): value is Record<string, any> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);

const deepCopy = <T,>(value: T): T => JSON.parse(JSON.stringify(value));

const formatDiffValue = (value: unknown) => {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) return 'undefined';
  return serialized.length > 180 ? `${serialized.slice(0, 177)}...` : serialized;
};

/** Build an RFC 7396 merge patch so omitted/cleared fields are deleted explicitly. */
const createMergePatch = (before: any, after: any): any => {
  if (!isPlainObject(before) || !isPlainObject(after)) {
    return JSON.stringify(before) === JSON.stringify(after) ? {} : after;
  }

  const patch: Record<string, any> = {};
  Object.keys(before).forEach((key) => {
    if (!Object.prototype.hasOwnProperty.call(after, key)) patch[key] = null;
  });
  Object.entries(after).forEach(([key, nextValue]) => {
    if (!Object.prototype.hasOwnProperty.call(before, key)) {
      patch[key] = nextValue;
      return;
    }
    const previousValue = before[key];
    if (isPlainObject(previousValue) && isPlainObject(nextValue)) {
      const nested = createMergePatch(previousValue, nextValue);
      if (Object.keys(nested).length > 0) patch[key] = nested;
    } else if (JSON.stringify(previousValue) !== JSON.stringify(nextValue)) {
      patch[key] = nextValue;
    }
  });
  return patch;
};

/** Split one canonical SNBI document into the Editor's main and child-table views. */
const editorRowsFromCanonical = (document: Record<string, any>) => {
  const children: Record<string, TableRow[]> = {};
  CHILD_TABLES.forEach((table) => { children[table] = []; });

  const features = Array.isArray(document.Features) ? document.Features : [];
  features.forEach((feature: Record<string, any>) => {
    const { Routes, ...featureFields } = feature;
    children.features.push(featureFields);
    (Array.isArray(Routes) ? Routes : []).forEach((route: Record<string, any>) => {
      children.routes.push({ ...route, BF01: feature.BF01 });
    });
  });
  CHILD_TABLES.filter((table) => table !== 'features' && table !== 'routes').forEach((table) => {
    const schemaKey = TABLE_NAME_TO_SCHEMA_KEY[table];
    children[table] = Array.isArray(document[schemaKey])
      ? document[schemaKey].map((row: TableRow) => ({ ...row }))
      : [];
  });
  const main = Object.fromEntries(
    SNBI_MAIN_FIELDS
      .filter((tag) => Object.prototype.hasOwnProperty.call(document, tag))
      .map((tag) => [tag, deepCopy(document[tag])]),
  );
  return { main, children };
};

const SCHEMA_KEY_TO_TABLE: Record<string, string> = Object.fromEntries(
  Object.entries(TABLE_NAME_TO_SCHEMA_KEY).map(([table, schemaKey]) => [schemaKey, table]),
);

/** Index validation attached to the exact revision into the existing field UI. */
const revisionValidationForEditor = (
  bridge: Bridge,
  canonical: Record<string, any>,
  rawIssues: unknown,
) => {
  const byTag: Record<string, ValidationIssue[]> = {};
  const byTable: Record<string, Record<string, Record<string, ValidationIssue[]>>> = {};
  const decodePointer = (pointer: string) => pointer
    .split('/')
    .slice(1)
    .map((part) => part.replace(/~1/g, '/').replace(/~0/g, '~'));
  const add = (issue: ValidationIssue, table: string | null, rowIndex: number | null) => {
    if (!issue.tag) return;
    if (!table || rowIndex === null) {
      (byTag[issue.tag] ||= []).push(issue);
      return;
    }
    byTable[table] ||= {};
    byTable[table][String(rowIndex)] ||= {};
    (byTable[table][String(rowIndex)][issue.tag] ||= []).push(issue);
  };

  (Array.isArray(rawIssues) ? rawIssues : []).forEach((raw: any) => {
    if (!isPlainObject(raw)) return;
    const parts = decodePointer(String(raw.json_pointer || ''));
    const tag = String(parts[parts.length - 1] || raw.tag || '');
    const rawSeverity = String(raw.severity || raw.type || 'warning').toLowerCase();
    const type: ValidationIssue['type'] = rawSeverity === 'critical' || rawSeverity === 'fatal'
      ? 'critical'
      : rawSeverity === 'error'
        ? 'error'
        : rawSeverity === 'safety'
          ? 'safety'
          : 'flag';
    let table: string | null = null;
    let rowIndex: number | null = null;
    if (parts[0] === 'Features' && /^\d+$/.test(parts[1] || '')) {
      const featureIndex = Number(parts[1]);
      if (parts[2] === 'Routes' && /^\d+$/.test(parts[3] || '')) {
        table = 'routes';
        rowIndex = (Array.isArray(canonical.Features) ? canonical.Features : [])
          .slice(0, featureIndex)
          .reduce((sum: number, feature: any) => sum + (Array.isArray(feature?.Routes) ? feature.Routes.length : 0), 0)
          + Number(parts[3]);
      } else {
        table = 'features';
        rowIndex = featureIndex;
      }
    } else if (SCHEMA_KEY_TO_TABLE[parts[0]] && /^\d+$/.test(parts[1] || '')) {
      table = SCHEMA_KEY_TO_TABLE[parts[0]];
      rowIndex = Number(parts[1]);
    }
    add({
      type,
      bridge: String(bridge.bridge_number),
      rule: String(raw.rule_id || raw.code || 'server_validation'),
      tag,
      message: String(raw.message || 'Server validation issue'),
      table,
      row_index: rowIndex,
      expected: raw.expected == null ? undefined : String(raw.expected),
      editable_hint: raw.editable_hint == null ? undefined : String(raw.editable_hint),
    }, table, rowIndex);
  });
  return { by_bridge: { [String(bridge.bridge_number)]: { by_tag: byTag, by_table: byTable } } };
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
  const [downloadingRevision, setDownloadingRevision] = useState(false);
  const [availableYears, setAvailableYears] = useState<number[]>([]);
  const [selectedExportYear, setSelectedExportYear] = useState<number | null>(null);
  const [bridgeValidation, setBridgeValidation] = useState<any | null>(null);
  const [fieldMetadata, setFieldMetadata] = useState<any | null>(null);
  const [publishedDocument, setPublishedDocument] = useState<Record<string, any> | null>(null);
  const [publishedRevisionId, setPublishedRevisionId] = useState<string | null>(null);
  const [publishedEtag, setPublishedEtag] = useState<string | null>(null);
  const [dataGeneration, setDataGeneration] = useState<number | null>(null);
  const [lastRevisionDiff, setLastRevisionDiff] = useState<any[]>([]);
  const [stagedValidation, setStagedValidation] = useState<any | null>(null);
  const [stagedValidationIssues, setStagedValidationIssues] = useState<any[]>([]);
  const [message, setMessage] = useState<{ type: 'success' | 'error' | 'info'; text: string } | null>(null);
  const loadRequestIdRef = useRef(0);

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

  const fetchFieldMetadata = async (
    bridge: Bridge,
    snapshotYear: number | null = selectedExportYear,
    requestId?: number,
  ) => {
    try {
      const res = await axios.get(
        `/api/bridge-field-metadata/${bridge.state_code}/${encodeURIComponent(bridge.bridge_number)}`,
        { params: { year: snapshotYear ?? undefined } },
      );
      if (requestId === undefined || requestId === loadRequestIdRef.current) {
        setFieldMetadata(res.data);
      }
    } catch (err) {
      console.error('Failed to load bridge field metadata:', err);
      if (requestId === undefined || requestId === loadRequestIdRef.current) {
        setFieldMetadata(null);
      }
    }
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
    // Preserve exact inventory/editor text. Only an all-whitespace input means
    // "clear this field"; canonical validation owns any further coercion.
    return value.trim() === '' ? '' : value;
  };

  const mapRowToSnbi = (row: Record<string, any>, tableName?: string) => {
    const out: Record<string, any> = {};
    const allowed = tableName ? TABLE_ALLOWED_SNBI_KEYS[tableName] : null;
    // Extensions are canonical, non-editor data. Preserve them byte-for-byte
    // through a field edit without rendering objects in text inputs.
    if (isPlainObject(row?.extensions)) out.extensions = deepCopy(row.extensions);
    Object.entries(row || {}).forEach(([key, value]) => {
      if (key === 'extensions') return;
      const snbiKey = COLUMN_TO_SNBI_KEY[key] || (allowed?.has(key) || (!allowed && SNBI_MAIN_FIELDS.includes(key)) ? key : undefined);
      if (!snbiKey) return;
      if (allowed && !allowed.has(snbiKey)) return;
      const cleaned = cleanValue(value);
      if (cleaned === null || cleaned === undefined || cleaned === '') return;
      out[snbiKey] = cleaned;
    });
    return out;
  };

  const buildCanonicalBridgeDocument = () => {
    if (!publishedDocument) throw new Error('Loaded inventory snapshot is unavailable.');
    // Start from the exact loaded object so fields outside the current UI,
    // including namespaced extensions, cannot be lost by an unrelated edit.
    const mainBridge: Record<string, any> = deepCopy(publishedDocument);
    const originalRows = editorRowsFromCanonical(publishedDocument);
    SNBI_MAIN_FIELDS.forEach((tag) => {
      if (tag === 'BL01' || tag === 'BID01') return;
      if (JSON.stringify(bridgeData?.[tag]) === JSON.stringify(originalRows.main[tag])) return;
      const cleaned = cleanValue(bridgeData?.[tag]);
      if (cleaned === null || cleaned === undefined || cleaned === '') delete mainBridge[tag];
      else mainBridge[tag] = cleaned;
    });

    CHILD_TABLES.filter((tableName) => tableName !== 'features' && tableName !== 'routes').forEach((tableName) => {
      if (JSON.stringify(childData[tableName] || []) === JSON.stringify(originalRows.children[tableName] || [])) return;
      const schemaKey = TABLE_NAME_TO_SCHEMA_KEY[tableName];
      if (!schemaKey) return;
      const mappedRows = (childData[tableName] || []).map((row) => mapRowToSnbi(row, tableName));
      mainBridge[schemaKey] = mappedRows;
    });

    const featuresChanged = JSON.stringify(childData.features || []) !== JSON.stringify(originalRows.children.features || []);
    const routesChanged = JSON.stringify(childData.routes || []) !== JSON.stringify(originalRows.children.routes || []);
    if (!featuresChanged && !routesChanged) return mainBridge;

    // Routes are nested under Features in SNBI schema. Attach by feature type (BF01).
    const routeRows = (childData.routes || []).map((row) => mapRowToSnbi(row, 'routes'));
    const routesByFeature = new Map<string, TableRow[]>();
    routeRows.forEach((route) => {
      const featureType = String(route.BF01 || '').trim();
      if (!featureType) throw new Error('Every route row must identify its parent feature in BF01 before staging.');
      const routeOnly = { ...route };
      delete routeOnly.BF01;
      if (!routesByFeature.has(featureType)) {
        routesByFeature.set(featureType, []);
      }
      routesByFeature.get(featureType)?.push(routeOnly);
    });

    const features = (childData.features || []).map((row) => mapRowToSnbi(row, 'features'));
    const originalFeatures = Array.isArray(publishedDocument.Features) ? publishedDocument.Features : [];
    features.forEach((feature: TableRow, featureIndex: number) => {
      const featureType = String(feature.BF01 || '').trim();
      if (!featureType) return;
      const routes = routesByFeature.get(featureType) || [];
      if (routes.length > 0) {
        feature.Routes = routes;
      } else {
        const original = originalFeatures.find((candidate: TableRow) =>
          String(candidate?.BF01 || '').trim() === featureType,
        ) || originalFeatures[featureIndex];
        if (original && Object.prototype.hasOwnProperty.call(original, 'Routes')) feature.Routes = [];
      }
      routesByFeature.delete(featureType);
    });

    if (routesByFeature.size > 0) {
      throw new Error(
        `Route rows reference missing parent feature(s): ${Array.from(routesByFeature.keys()).join(', ')}. Remove them or assign an existing BF01.`,
      );
    }
    mainBridge.Features = features;

    return mainBridge;
  };

  const downloadPublishedRevisionPackage = async () => {
    if (!publishedRevisionId || !selectedBridge) {
      setMessage({ type: 'error', text: 'Reload the exact inventory snapshot before exporting.' });
      return;
    }
    setDownloadingRevision(true);
    try {
      const payload = new Blob(
        [JSON.stringify([publishedDocument], null, 2)],
        { type: 'application/json' },
      );
      const fileName = `${buildBaseFileName()}_${selectedExportYear ?? selectedBridge.data_year}_inventory.json`;
      triggerFileDownload(payload, fileName);
      setMessage({
        type: 'success',
        text: 'Downloaded the exact loaded inventory snapshot as SNBI JSON.',
      });
    } catch (err: any) {
      console.error('Failed to download inventory snapshot:', err);
      setMessage({ type: 'error', text: 'Failed to download the inventory snapshot.' });
    } finally {
      setDownloadingRevision(false);
    }
  };

  // Load the selector directly from exact yearly inventory rows.
  useEffect(() => {
    if (!selectedStateCode || selectedExportYear === null) {
      setBridges([]);
      return;
    }

    let cancelled = false;
    const fetchBridges = async () => {
      setLoading(true);
      try {
        const response: AxiosResponse<any> = await axios.get('/api/bridge-snapshots', {
          params: { state_code: selectedStateCode, year: selectedExportYear, limit: 10000 },
        });
        const rows = Array.isArray(response.data?.snapshots) ? response.data.snapshots : [];
        if (!cancelled) setBridges(rows);
      } catch (err) {
        console.error(err);
        if (!cancelled) {
          setBridges([]);
          setMessage({
            type: 'error',
            text: err instanceof Error ? err.message : 'Failed to load inventory bridges',
          });
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchBridges();
    return () => { cancelled = true; };
  }, [selectedStateCode, selectedExportYear]);

  // Year facets come directly from the loaded bridge snapshots.
  useEffect(() => {
    if (!selectedStateCode) {
      setAvailableYears([]);
      setSelectedExportYear(null);
      return;
    }
    axios.get(`/api/available-years/${selectedStateCode}`)
      .then((res) => {
        const years = Array.isArray(res.data?.years)
          ? res.data.years.filter((year: unknown) => Number.isSafeInteger(year))
          : [];
        setAvailableYears(years);
        setSelectedExportYear(years[0] ?? null);
      })
      .catch(() => {
        setAvailableYears([]);
        setSelectedExportYear(null);
      });
  }, [selectedStateCode]);

  // Load full bridge data when a bridge is selected
  const loadBridgeDetails = async (
    bridge: Bridge,
    snapshotYear: number | null = selectedExportYear,
  ) => {
    const requestId = ++loadRequestIdRef.current;
    setSelectedBridge(bridge);
    setLoading(true);
    setBridgeData(null);
    setChildData({});
    setBridgeValidation(null);
    setFieldMetadata(null);
    setPublishedDocument(null);
    setPublishedRevisionId(null);
    setPublishedEtag(null);
    setDataGeneration(null);
    setLastRevisionDiff([]);
    setStagedValidation(null);
    setStagedValidationIssues([]);

    try {
      if (snapshotYear === null) {
        throw new Error('Select an exact inventory year before editing.');
      }
      const response = await axios.get(
        `/api/bridge-document/${bridge.state_code}/${encodeURIComponent(bridge.bridge_number)}/${snapshotYear}`,
      );
      if (requestId !== loadRequestIdRef.current) return;
      const canonical = response.data?.canonical_document;
      if (!isPlainObject(canonical)) throw new Error('Loaded inventory snapshot is unavailable.');
      const revisionId = String(response.data?.snapshot_id || '');
      const etag = String(response.data?.etag || response.headers.etag || '');
      const generation = Number(response.data?.data_generation);
      if (
        !revisionId
        || !/^"[^"\r\n]+"$/.test(etag)
        || !Number.isSafeInteger(generation)
        || generation < 0
      ) {
        throw new Error('Inventory snapshot concurrency metadata is unavailable.');
      }
      const editorRows = editorRowsFromCanonical(canonical);
      setPublishedDocument(deepCopy(canonical));
      setPublishedRevisionId(revisionId);
      setPublishedEtag(etag);
      setDataGeneration(generation);
      setBridgeData(editorRows.main);
      setChildData(editorRows.children);
      setBridgeValidation(
        revisionValidationForEditor(
          bridge,
          canonical,
          response.data?.validation?.issues || response.data?.validation?.validation_issues,
        ),
      );
      await fetchFieldMetadata(bridge, snapshotYear, requestId);

    } catch (err: any) {
      if (requestId !== loadRequestIdRef.current) return;
      console.error('Failed to load bridge details:', err);
      setMessage({
        type: 'error',
        text: `Failed to load bridge details: ${err.response?.data?.detail || err.message}`
      });
      setBridgeData(null);
      setChildData({});
      setFieldMetadata(null);
      setPublishedDocument(null);
      setPublishedRevisionId(null);
      setPublishedEtag(null);
      setDataGeneration(null);
    } finally {
      if (requestId === loadRequestIdRef.current) {
        setLoading(false);
      }
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
    if (!selectedBridge || !bridgeData || !publishedDocument || !publishedEtag || dataGeneration === null || selectedExportYear === null) {
      setMessage({ type: 'error', text: 'Reload the exact inventory snapshot before saving edits.' });
      return;
    }

    let canonicalDocument: Record<string, any>;
    try {
      canonicalDocument = buildCanonicalBridgeDocument();
    } catch (err: any) {
      setMessage({ type: 'error', text: err?.message || 'The canonical document could not be built.' });
      return;
    }
    const patch = createMergePatch(publishedDocument, canonicalDocument);
    if (!isPlainObject(patch) || Object.keys(patch).length === 0) {
      setMessage({ type: 'info', text: 'No canonical changes were detected.' });
      return;
    }

    setSaving(true);
    setLastRevisionDiff([]);
    setStagedValidation(null);
    setStagedValidationIssues([]);
    setMessage({ type: 'info', text: 'Validating and saving the complete yearly inventory snapshot...' });
    try {
      const response = await axios.put(
        `/api/bridge-document/${selectedBridge.state_code}/${encodeURIComponent(selectedBridge.bridge_number)}/${selectedExportYear}`,
        { canonical_document: canonicalDocument, expected_data_generation: dataGeneration },
        {
          headers: {
            'If-Match': publishedEtag,
          },
        },
      );
      const savedDocument = response.data?.canonical_document;
      const issues = response.data?.validation?.issues || response.data?.validation?.validation_issues || [];
      if (isPlainObject(savedDocument)) {
        const editorRows = editorRowsFromCanonical(savedDocument);
        setPublishedDocument(deepCopy(savedDocument));
        setBridgeData(editorRows.main);
        setChildData(editorRows.children);
      }
      setPublishedEtag(String(response.data?.etag || response.headers.etag || publishedEtag));
      setDataGeneration(Number(response.data?.data_generation ?? dataGeneration));
      setStagedValidation(response.data?.validation || { status: 'unknown' });
      setStagedValidationIssues(Array.isArray(issues) ? issues : []);
      setMessage({
        type: 'success',
        text: `Saved the ${selectedExportYear} inventory snapshot directly to snbi_bridges.db. History, forecasts, LCCA, KPIs, maps, and Bridgette now read this updated row.`,
      });
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const text = typeof detail === 'string'
        ? detail
        : detail?.message || detail?.code || err.message || 'Failed to save the inventory snapshot';
      setMessage({ type: 'error', text });
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => {
    if (selectedBridge) {
      loadBridgeDetails(selectedBridge, selectedExportYear);
    }
    // Reload the complete snapshot—not just its annotations—when the year changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedExportYear]);

  const handleChildTableChange = (table: string, index: number, field: string, value: any) => {
    const updated = [...(childData[table] || [])];
    updated[index] = { ...updated[index], [field]: value };
    setChildData(prev => ({ ...prev, [table]: updated }));
  };

  // Append a canonical SNBI row for the active collection. It remains local
  // until the complete exact-year bridge document is saved.
  const addChildRow = () => {
    const template = Object.fromEntries(
      Array.from(TABLE_ALLOWED_SNBI_KEYS[activeTab] || []).map((tag) => [tag, '']),
    );
    setChildData((prev) => ({ ...prev, [activeTab]: [...(prev[activeTab] || []), template] }));
  };

  const removeChildRow = (table: string, index: number) => {
    setChildData((prev) => ({
      ...prev,
      [table]: (prev[table] || []).filter((_, rowIndex) => rowIndex !== index),
    }));
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
            onChange={(e) => {
              loadRequestIdRef.current += 1;
              setSelectedStateCode(Number(e.target.value));
              setSelectedExportYear(null);
              setSelectedBridge(null);
              setBridgeData(null);
              setChildData({});
              setBridgeValidation(null);
              setFieldMetadata(null);
              setPublishedDocument(null);
              setPublishedRevisionId(null);
              setPublishedEtag(null);
              setDataGeneration(null);
              setLastRevisionDiff([]);
              setStagedValidation(null);
              setStagedValidationIssues([]);
            }}
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

      {/* Exact canonical inventory year — shown whenever a state is selected */}
      {selectedStateCode && (
        <div className="glass rounded-3xl p-6 shadow-sm">
          <div className="flex flex-col md:flex-row md:items-end gap-4">
            <div className="flex-1">
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                SNBI Snapshot &amp; NBI Source Year
              </label>
              {availableYears.length > 0 ? (
                <select
                  value={selectedExportYear ?? ''}
                  onChange={(e) => {
                    loadRequestIdRef.current += 1;
                    setSelectedExportYear(Number(e.target.value));
                  }}
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
                The selected year scopes the bridge values, child tables, NBI provenance, validation, saves, and exports together.
              </p>
            </div>
            <div className="max-w-md rounded-2xl border border-sky-200 bg-sky-50 px-4 py-3 text-xs text-sky-800 dark:border-sky-800 dark:bg-sky-950/30 dark:text-sky-200">
              Select a bridge to edit or download its exact yearly SNBI inventory snapshot.
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
              {selectedExportYear && (
                <div className="mx-8 mt-6 flex flex-wrap items-center gap-2 rounded-2xl border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900 dark:border-sky-800 dark:bg-sky-950/40 dark:text-sky-200">
                  <span className="font-semibold">SNBI snapshot {selectedExportYear}</span>
                  <span aria-hidden="true">·</span>
                  <span>NBI source notes {selectedExportYear}</span>
                  <span className="text-xs text-sky-700 dark:text-sky-300">
                    Every value shown or saved below uses this same inventory year.
                  </span>
                </div>
              )}
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
                    onClick={downloadPublishedRevisionPackage}
                    disabled={downloadingRevision || !publishedRevisionId}
                    className="flex items-center gap-2 text-sm text-violet-600 hover:bg-violet-50 dark:hover:bg-violet-900/20 px-3 py-2 rounded-xl border border-violet-200 dark:border-violet-800 disabled:opacity-50"
                  >
                    {downloadingRevision ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                    {downloadingRevision ? 'Preparing JSON…' : 'Inventory JSON'}
                  </button>
                  <button
                    onClick={handleSaveMainBridge}
                    disabled={saving}
                    className="flex items-center gap-2 bg-violet-600 hover:bg-violet-700 text-white px-5 py-2 rounded-xl font-semibold shadow-sm disabled:opacity-70 transition-all"
                  >
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    {saving ? "Saving…" : "Save All Edits"}
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
                      disabled={key === 'BL01' || key === 'BID01'}
                      title={issues[0]?.expected || metadata[0]?.source_value || metadata[0]?.note || undefined}
                      className={`w-full bg-gray-50 dark:bg-gray-900 border rounded-xl px-4 py-3 text-sm text-gray-900 dark:text-white focus:outline-none focus:ring-2 disabled:cursor-not-allowed disabled:opacity-60 ${issueClasses(issues)}`}
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
                    onClick={downloadPublishedRevisionPackage}
                    disabled={downloadingRevision || !publishedRevisionId}
                    className="flex items-center gap-2 text-sm text-violet-600 hover:bg-violet-50 dark:hover:bg-violet-900/20 px-3 py-2 rounded-xl border border-violet-200 dark:border-violet-800 disabled:opacity-50"
                  >
                    {downloadingRevision ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                    {downloadingRevision ? 'Preparing JSON…' : 'Inventory JSON'}
                  </button>
                  <button
                    onClick={addChildRow}
                    className="flex items-center gap-2 bg-violet-600 hover:bg-violet-700 text-white px-4 py-2 rounded-xl font-semibold shadow-sm"
                  >
                    <Plus className="w-4 h-4" /> Add Row
                  </button>
                  <button
                    onClick={handleSaveMainBridge}
                    disabled={saving}
                    className="flex items-center gap-2 bg-violet-600 hover:bg-violet-700 text-white px-4 py-2 rounded-xl font-semibold shadow-sm disabled:opacity-70"
                  >
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    {saving ? 'Saving…' : 'Save All Edits'}
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
                        {Array.from(TABLE_ALLOWED_SNBI_KEYS[activeTab] || []).map(col => (
                          <th key={col} className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">{getFieldLabel(col)}</th>
                        ))}
                        <th className="px-4 py-3 text-right font-medium text-gray-700 dark:text-gray-300">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {childData[activeTab].map((row, idx) => (
                        <tr key={idx} className="border-t border-gray-100 dark:border-gray-800">
                          {Array.from(TABLE_ALLOWED_SNBI_KEYS[activeTab] || []).map((key) => {
                            const issues = getChildIssues(activeTab, idx, key);
                            const metadata = getChildMetadata(activeTab, idx, key);
                            return (
                            <td key={key} className="px-4 py-3 align-top">
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
                          <td className="px-4 py-3 text-right align-top">
                            <button
                              type="button"
                              onClick={() => removeChildRow(activeTab, idx)}
                              className="inline-flex items-center gap-1 rounded-lg border border-red-200 px-2 py-1 text-xs text-red-700 hover:bg-red-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950/30"
                              aria-label={`Remove ${activeTab.replace('_', ' ')} row ${idx + 1}`}
                            >
                              <Trash2 className="h-3.5 w-3.5" /> Remove
                            </button>
                          </td>
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
      {lastRevisionDiff.length > 0 && (
        <div className="rounded-2xl border border-violet-200 bg-violet-50 p-5 text-violet-900 dark:border-violet-800 dark:bg-violet-950/30 dark:text-violet-100">
          <p className="font-semibold">Saved snapshot changes</p>
          <ul className="mt-2 max-h-48 space-y-1 overflow-auto text-xs font-mono">
            {lastRevisionDiff.slice(0, 100).map((change, index) => (
              <li key={`${change.path || 'change'}-${index}`}>
                {String(change.op || 'replace').toUpperCase()} {String(change.path || '/')}
                {Object.prototype.hasOwnProperty.call(change, 'before') && (
                  <span className="block pl-3 text-violet-700 dark:text-violet-300">before: {formatDiffValue(change.before)}</span>
                )}
                {Object.prototype.hasOwnProperty.call(change, 'after') && (
                  <span className="block pl-3 text-violet-700 dark:text-violet-300">after: {formatDiffValue(change.after)}</span>
                )}
              </li>
            ))}
          </ul>
          {lastRevisionDiff.length > 100 && <p className="mt-2 text-xs">Only the first 100 changes are shown.</p>}
        </div>
      )}
      {stagedValidation && (
        <div className="rounded-2xl border border-sky-200 bg-sky-50 p-5 text-sky-900 dark:border-sky-800 dark:bg-sky-950/30 dark:text-sky-100">
          <p className="font-semibold">Server validation result</p>
          <p className="mt-1 text-sm">
            Status: <span className="font-semibold">{String(stagedValidation.status || 'unknown')}</span>
            {' · '}Owner: {String(stagedValidation.validation_owner || 'server')}
            {' · '}Issues: {stagedValidationIssues.length}
          </p>
          {stagedValidation.official_validation_status && (
            <p className="mt-1 text-xs">Official-profile validation: {String(stagedValidation.official_validation_status)}</p>
          )}
        </div>
      )}
      {stagedValidationIssues.length > 0 && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-100">
          <p className="font-semibold">Server validation evidence ({stagedValidationIssues.length})</p>
          <div className="mt-2 space-y-1 text-xs">
            {stagedValidationIssues.slice(0, 20).map((issue, index) => (
              <p key={`${issue.code || issue.rule_id || issue.rule || 'issue'}-${index}`}>
                <span className="font-semibold uppercase">{issue.severity || issue.type || 'review'}</span>{' '}
                {issue.json_pointer || issue.tag || '/'}: {issue.message || issue.code}
              </p>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};

export default Editor;
