// frontend/src/pages/Converter.tsx
import React, { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import { Upload, FileText, Download, AlertCircle, CheckCircle, ArrowRightLeft, ChevronDown, Loader2, Settings2 } from 'lucide-react';
import { SectionHeader } from '../components/ui';

interface ConvertResponse {
  status: string;
  bridges: any[];
  converted: number;
  skipped: number;
  elements_from_xml?: boolean;
  parity_ok?: boolean;
  parity_errors?: string[];
  coverage?: {
    total_schema_codes: number;
    present_code_count: number;
    missing_code_count: number;
    present_codes: string[];
    missing_codes: string[];
  };
  section_coverage?: Record<string, {
    total_codes: number;
    present_code_count: number;
    missing_code_count: number;
    present_codes: string[];
    missing_codes: string[];
  }>;
  validation?: any;
  canonical_normalization?: {
    profile_id: string;
    submission_ready: boolean;
    documents: Array<{
      document_id: string;
      document_sha256: string;
      schema_status: string;
      diagnostics: any[];
    }>;
  };
  diagnostics?: {
    format: string;
    media_type: string;
    filename: string;
    sha256: string;
    issues: any[];
    canonical_normalization: any[];
    validation_reports: any[];
  };
  engine?: string;
  conversion_profile?: string;
  submission_ready?: boolean;
  submission_label?: string;
  normalization_issues?: string[];
  parser?: {
    format: string;
    encoding: string;
    source_sha256: string;
    records_seen: number;
    records_accepted: number;
    records_quarantined: number;
    bridge_groups: number;
    issues: any[];
  };
  sources?: {
    nbi?: { artifact_id: string; sha256: string; size_bytes: number };
    elements?: { artifact_id: string; sha256: string; size_bytes: number } | null;
  };
  transition_coverage?: {
    bridge_count: number;
    document_leaf_count: number;
    lineage_leaf_count: number;
    populated_permanent_count: number;
    populated_temporary_count: number;
    null_source_count: number;
    invalid_source_count: number;
    no_transition_count: number;
    uncovered_leaf_count: number;
  };
  eligibility?: {
    bridge_groups: number;
    federally_eligible_bridge_groups: number;
    research_retained_bridge_groups: number;
    rule_status: string;
  };
  bid01_preview?: {
    policy: string;
    can_apply: boolean;
    entry_count: number;
    changed_count: number;
  };
  inspection_history?: {
    status: 'ready' | 'blocked';
    history_ready: boolean;
    rule_bundle_id: string;
    history_scope: {
      start_inventory_year: number;
      end_inventory_year: number;
      target_inventory_year: number;
      first_occurrence_semantics: string;
      pre_2000_compatibility_enabled: false;
    };
    coverage: {
      source_artifact_count: number;
      target_bridge_count: number;
      ready_target_bridge_count: number;
      blocked_target_bridge_count: number;
      event_count: number;
      deduplicated_event_count: number;
      issue_severity_counts: Record<string, number>;
      not_invented_tags: string[];
    };
    bridges: Array<{ issues: Array<{ code: string; message: string }> }>;
  };
}

interface ReverseConvertResponse {
  status: string;
  flat_json: any[];
  text: string;
  nbi_csv: string;
  elements_xml: string;
  count: number;
  legacy_projection: {
    kind: 'legacy_projection_for_review';
    format: string;
    label: string;
    media_type: 'text/csv';
    suggested_extension: '.csv';
    official_nbi_445: false;
    bridge_count: number;
    legacy_row_count: number;
    row_policy: string;
  };
  loss_manifest: {
    manifest_version: string;
    projection_format: string;
    label: string;
    exact_reverse: false;
    bridge_count: number;
    legacy_row_count: number;
    row_policy: string;
    classification_counts: Record<string, number>;
    bridges: Array<{
      bridge_index: number;
      bridge_number: string;
      legacy_row_count: number;
      record_types: string[];
      outcomes: Array<{
        source_path: string;
        source_field: string;
        legacy_field: string | null;
        classification: string;
        value_written: string | null;
        rule_id: string;
        message: string;
      }>;
    }>;
  };
  warnings?: string[];
}

type TabMode = 'nbi-to-snbi' | 'snbi-to-nbi';
type ForwardMode = 'single' | 'annual-history';
type AnnualSourceSelection = { file: File; inventoryYear: string };

const FIPS_STATE_NAMES: Record<number, string> = {
  1: 'Alabama', 2: 'Alaska', 4: 'Arizona', 5: 'Arkansas', 6: 'California',
  8: 'Colorado', 9: 'Connecticut', 10: 'Delaware', 11: 'District_of_Columbia',
  12: 'Florida', 13: 'Georgia', 15: 'Hawaii', 16: 'Idaho', 17: 'Illinois',
  18: 'Indiana', 19: 'Iowa', 20: 'Kansas', 21: 'Kentucky', 22: 'Louisiana',
  23: 'Maine', 24: 'Maryland', 25: 'Massachusetts', 26: 'Michigan',
  27: 'Minnesota', 28: 'Mississippi', 29: 'Missouri', 30: 'Montana',
  31: 'Nebraska', 32: 'Nevada', 33: 'New_Hampshire', 34: 'New_Jersey',
  35: 'New_Mexico', 36: 'New_York', 37: 'North_Carolina', 38: 'North_Dakota',
  39: 'Ohio', 40: 'Oklahoma', 41: 'Oregon', 42: 'Pennsylvania',
  44: 'Rhode_Island', 45: 'South_Carolina', 46: 'South_Dakota', 47: 'Tennessee',
  48: 'Texas', 49: 'Utah', 50: 'Vermont', 51: 'Virginia', 53: 'Washington',
  54: 'West_Virginia', 55: 'Wisconsin', 56: 'Wyoming', 72: 'Puerto_Rico',
  78: 'Virgin_Islands',
};

function getStateName(bridges: any[]): string {
  const code = Number(bridges?.[0]?.BL01 ?? bridges?.[0]?.state_code);
  return FIPS_STATE_NAMES[code] ?? (code ? String(code) : 'state');
}

const STORAGE_KEY_TAB = 'converter_active_tab';

function apiErrorMessage(error: any, fallback: string): string {
  const detail = error?.response?.data?.detail ?? error?.response?.data?.error;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const message = typeof detail.message === 'string' ? detail.message : fallback;
    const issue = typeof detail.issue?.message === 'string' ? detail.issue.message : '';
    return issue ? `${message} ${issue}` : message;
  }
  return error?.message ? `Request failed: ${error.message}` : fallback;
}

try {
  sessionStorage.removeItem('converter_result');
  sessionStorage.removeItem('converter_rev_result');
} catch {}

// Shared building blocks ----------------------------------------------------
const Dropzone: React.FC<{ id: string; accept: string; file: File | null; label: string; onChange: (e: React.ChangeEvent<HTMLInputElement>) => void }> = ({ id, accept, file, label, onChange }) => (
  <label htmlFor={id} className="flex flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed border-slate-300 hover:border-brand-400 hover:bg-slate-50 transition-colors cursor-pointer py-8 px-6 text-center">
    <Upload className="w-9 h-9 text-slate-400" />
    <span className="text-sm font-semibold text-slate-700">{file ? file.name : label}</span>
    <input id={id} type="file" accept={accept} onChange={onChange} className="hidden" />
  </label>
);

const ErrorCard: React.FC<{ msg: string }> = ({ msg }) => (
  <div className="rounded-2xl bg-red-50 border border-red-200 p-4 flex gap-3">
    <AlertCircle className="w-5 h-5 text-red-600 mt-0.5 shrink-0" />
    <div>
      <p className="font-medium text-red-800">Conversion failed</p>
      <p className="text-sm text-red-700 mt-0.5">{msg}</p>
    </div>
  </div>
);

const Converter: React.FC = () => {
  const [activeTab, setActiveTab] = useState<TabMode>(
    (sessionStorage.getItem(STORAGE_KEY_TAB) as TabMode) || 'nbi-to-snbi'
  );

  // Forward conversion state
  const [txtFile, setTxtFile] = useState<File | null>(null);
  const [xmlFile, setXmlFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ConvertResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedCoverageSection, setSelectedCoverageSection] = useState<string>('');
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [sourceAgency, setSourceAgency] = useState('NJDOT');
  const [inputFormat, setInputFormat] = useState('fhwa_fixed_445');
  const [encoding, setEncoding] = useState('ascii');
  const [issuePolicy, setIssuePolicy] = useState('reject');
  const [bid01Policy, setBid01Policy] = useState('preserve');
  const [csvDelimiter, setCsvDelimiter] = useState(',');
  const [csvQuotechar, setCsvQuotechar] = useState("'");
  const [forwardMode, setForwardMode] = useState<ForwardMode>('single');
  const [annualSources, setAnnualSources] = useState<AnnualSourceSelection[]>([]);
  const [targetInventoryYear, setTargetInventoryYear] = useState('');

  // Reverse conversion state
  const [snbiFile, setSnbiFile] = useState<File | null>(null);
  const [revLoading, setRevLoading] = useState(false);
  const [revResult, setRevResult] = useState<ReverseConvertResponse | null>(null);
  const [revError, setRevError] = useState<string | null>(null);

  const sectionCoverageEntries = useMemo(
    () => Object.entries(result?.section_coverage ?? {}),
    [result?.section_coverage]
  );

  useEffect(() => {
    if (sectionCoverageEntries.length === 0) {
      if (selectedCoverageSection !== '') setSelectedCoverageSection('');
      return;
    }
    const hasCurrent = sectionCoverageEntries.some(([name]) => name === selectedCoverageSection);
    if (!hasCurrent) setSelectedCoverageSection(sectionCoverageEntries[0][0]);
  }, [sectionCoverageEntries, selectedCoverageSection]);

  const switchTab = (tab: TabMode) => { setActiveTab(tab); sessionStorage.setItem(STORAGE_KEY_TAB, tab); };

  const selectForwardMode = (mode: ForwardMode) => {
    setForwardMode(mode);
    setError(null);
    setResult(null);
    setDownloadUrl(null);
  };

  const annualYears = annualSources
    .map((item) => Number(item.inventoryYear))
    .filter((year) => Number.isInteger(year) && year >= 1900 && year <= 2199);
  const annualHistoryProblem = (() => {
    if (annualSources.length < 2) return 'Select at least two annual NBI source files.';
    if (annualYears.length !== annualSources.length) return 'Enter an inventory year from 1900 to 2199 for every file.';
    if (new Set(annualYears).size !== annualYears.length) return 'Each annual file must have a unique inventory year.';
    const start = Math.min(...annualYears);
    const end = Math.max(...annualYears);
    if (end - start + 1 !== annualYears.length) return 'Annual files must cover every contiguous inventory year.';
    if (!annualYears.includes(Number(targetInventoryYear))) return 'Select the target inventory year to convert.';
    return null;
  })();

  const handleAnnualFiles = (event: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files ?? []);
    const next = selected.map((file) => {
      const match = file.name.match(/(?:19|20|21)\d{2}/);
      return { file, inventoryYear: match?.[0] ?? '' };
    });
    setAnnualSources(next);
    const inferredYears = next
      .map((item) => Number(item.inventoryYear))
      .filter((year) => Number.isInteger(year));
    setTargetInventoryYear(inferredYears.length ? String(Math.max(...inferredYears)) : '');
    setError(null);
    setResult(null);
  };

  const handleHistoryConvert = async () => {
    if (annualHistoryProblem) { setError(annualHistoryProblem); return; }
    if (!sourceAgency.trim()) { setError('Enter the source agency.'); return; }
    if (!bid01Policy) { setError('Select a BID01 leading-zero policy.'); return; }
    const start = Math.min(...annualYears);
    const end = Math.max(...annualYears);
    const historyManifest: Record<string, any> = {
      manifest_version: 'bridgeguard-nbi-annual-history-v1',
      source_agency_context: sourceAgency.trim(),
      encoding,
      input_format: inputFormat,
      issue_policy: issuePolicy,
      bid01_policy: bid01Policy,
      rule_bundle_id: 'fhwa-nbi-to-snbi-inspection-events-v1',
      history_start_year: start,
      history_end_year: end,
      target_inventory_year: Number(targetInventoryYear),
      annual_sources: annualSources.map((item) => ({
        part_name: item.file.name,
        inventory_year: Number(item.inventoryYear),
      })),
    };
    if (inputFormat !== 'fhwa_fixed_445') {
      historyManifest.csv_dialect = { delimiter: csvDelimiter, quotechar: csvQuotechar };
    }
    const formData = new FormData();
    formData.append('manifest', JSON.stringify(historyManifest));
    annualSources.forEach((item) => formData.append('annual_files', item.file));
    setLoading(true);
    setError(null);
    setResult(null);
    setDownloadUrl(null);
    try {
      const response = await axios.post<ConvertResponse>('/api/convert-v2/inspection-history', formData, { headers: { 'Content-Type': 'multipart/form-data' } });
      setResult(response.data);
      if (response.data.bridges.length > 0) {
        const jsonBlob = new Blob([JSON.stringify(response.data.bridges, null, 2)], { type: 'application/json' });
        setDownloadUrl(URL.createObjectURL(jsonBlob));
      }
    } catch (err: any) {
      setError(apiErrorMessage(err, 'Failed to reconstruct annual inspection history.'));
    } finally {
      setLoading(false);
    }
  };

  const handleConvert = async () => {
    if (!txtFile) { setError('Please upload an NBI TXT file'); return; }
    if (!bid01Policy) { setError('Select a BID01 leading-zero policy.'); return; }
    setLoading(true);
    setError(null);
    setResult(null);
    setDownloadUrl(null);
    const formData = new FormData();
    formData.append('txt_file', txtFile);
    if (xmlFile) formData.append('xml_file', xmlFile);
    formData.append('source_agency_context', sourceAgency.trim());
    formData.append('encoding', encoding);
    formData.append('input_format', inputFormat);
    formData.append('issue_policy', issuePolicy);
    formData.append('conversion_profile', 'research_historical_import');
    formData.append('bid01_policy', bid01Policy);
    if (inputFormat !== 'fhwa_fixed_445') {
      formData.append('csv_delimiter', csvDelimiter);
      formData.append('csv_quotechar', csvQuotechar);
    }
    try {
      const response = await axios.post<ConvertResponse>('/api/convert-v2', formData, { headers: { 'Content-Type': 'multipart/form-data' } });
      setResult(response.data);
      const jsonBlob = new Blob([JSON.stringify(response.data.bridges, null, 2)], { type: 'application/json' });
      setDownloadUrl(URL.createObjectURL(jsonBlob));
    } catch (err: any) {
      setError(apiErrorMessage(err, 'Failed to convert files.'));
    } finally {
      setLoading(false);
    }
  };

  const handleDownload = () => {
    if (!downloadUrl) return;
    const stateName = result ? getStateName(result.bridges) : 'state';
    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = `snbi_bridges_${stateName}_${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleDiagnosticsDownload = () => {
    if (!result?.diagnostics) return;
    const blob = new Blob(
      [JSON.stringify(result.diagnostics, null, 2)],
      { type: result.diagnostics.media_type || 'application/json' },
    );
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = result.diagnostics.filename || 'bridgeguard-conversion-diagnostics.json';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const handleSnbiFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setSnbiFile(e.target.files[0]);
      setRevError(null);
      setRevResult(null);
    }
  };

  const handleReverseConvert = async () => {
    if (!snbiFile) { setRevError('Please upload an SNBI JSON file'); return; }
    setRevLoading(true);
    setRevError(null);
    setRevResult(null);
    const formData = new FormData();
    formData.append('file', snbiFile);
    try {
      const response = await axios.post<ReverseConvertResponse>('/api/reverse-convert', formData, { headers: { 'Content-Type': 'multipart/form-data' } });
      setRevResult(response.data);
    } catch (err: any) {
      setRevError(apiErrorMessage(err, 'Failed to generate review projections.'));
    } finally {
      setRevLoading(false);
    }
  };

  const downloadRev = (kind: 'csv' | 'xml' | 'loss') => {
    if (!revResult) return;
    const stateName = getStateName(revResult.flat_json);
    const date = new Date().toISOString().slice(0, 10);
    const download = kind === 'csv'
      ? {
          body: revResult.nbi_csv,
          mediaType: revResult.legacy_projection?.media_type || 'text/csv',
          filename: `legacy_projection_${stateName}_${date}.csv`,
        }
      : kind === 'loss'
        ? {
            body: JSON.stringify(revResult.loss_manifest, null, 2),
            mediaType: 'application/json',
            filename: `legacy_projection_loss_report_${stateName}_${date}.json`,
          }
        : {
            body: revResult.elements_xml,
            mediaType: 'application/xml',
            filename: `elements_projection_${stateName}_${date}.xml`,
          };
    const blob = new Blob([download.body], { type: download.mediaType });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = download.filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  const primaryBtn = 'inline-flex items-center justify-center gap-2 rounded-xl bg-brand-600 hover:bg-brand-700 text-white font-semibold py-3 px-6 transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
  const secondaryBtn = 'inline-flex items-center justify-center gap-2 rounded-xl border border-slate-300 bg-white hover:bg-slate-50 text-slate-700 font-semibold py-3 px-6 transition-colors';
  const tabBtn = (active: boolean) =>
    `inline-flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-medium transition-colors ${
      active ? 'bg-brand-600 text-white shadow-sm' : 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'
    }`;

  const forwardAdvancedOptions = (
    <details className="rounded-2xl border border-slate-200 bg-slate-50">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-semibold text-slate-700">
        <Settings2 className="h-4 w-4 text-slate-500" />
        Advanced options
        <span className="ml-1 text-xs font-normal text-slate-500">Use the defaults for standard NJDOT files.</span>
        <ChevronDown className="ml-auto h-4 w-4" />
      </summary>
      <div className="border-t border-slate-200 p-4">
        <p className="mb-4 text-xs leading-5 text-slate-600">
          Change these only when the source uses a different agency, layout, text encoding, or bridge-number convention. Incorrect format settings can prevent records from being read correctly.
        </p>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <label className="space-y-1.5 text-sm font-medium text-slate-700">
            Source agency
            <input value={sourceAgency} onChange={(event) => setSourceAgency(event.target.value)} className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 font-normal text-slate-900" maxLength={80} required />
          </label>
          <label className="space-y-1.5 text-sm font-medium text-slate-700">
            Source file layout
            <select value={inputFormat} onChange={(event) => setInputFormat(event.target.value)} className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 font-normal text-slate-900">
              <option value="fhwa_fixed_445">FHWA fixed width (445 bytes)</option>
              <option value="fhwa_download_csv">FHWA NBI download CSV</option>
              <option value="named_csv">CSV with named header</option>
              <option value="legacy_positional_csv">Legacy positional CSV</option>
            </select>
          </label>
          <label className="space-y-1.5 text-sm font-medium text-slate-700">
            Text encoding
            <select value={encoding} onChange={(event) => setEncoding(event.target.value)} className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 font-normal text-slate-900">
              <option value="ascii">ASCII</option>
              <option value="utf-8">UTF-8</option>
              <option value="utf-8-sig">UTF-8 with BOM</option>
              <option value="cp1252">Windows-1252</option>
              <option value="iso8859-1">ISO-8859-1</option>
            </select>
          </label>
          <label className="space-y-1.5 text-sm font-medium text-slate-700">
            Invalid record handling
            <select value={issuePolicy} onChange={(event) => setIssuePolicy(event.target.value)} className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 font-normal text-slate-900">
              <option value="reject">Stop the conversion</option>
              <option value="quarantine">Skip and report invalid records</option>
              <option value="accept_with_issue">Convert and flag them</option>
            </select>
          </label>
          <label className="space-y-1.5 text-sm font-medium text-slate-700">
            Bridge-number leading zeros
            <select value={bid01Policy} onChange={(event) => setBid01Policy(event.target.value)} className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 font-normal text-slate-900">
              <option value="preserve">Preserve exactly</option>
              <option value="trim_all_leading_zeros">Remove all leading zeros</option>
              <option value="trim_all_but_one">Keep one leading zero</option>
            </select>
            <span className="block text-xs font-normal text-slate-500">Preserving the source identifier is the safest default.</span>
          </label>
          {inputFormat !== 'fhwa_fixed_445' && (
            <>
              <label className="space-y-1.5 text-sm font-medium text-slate-700">
                CSV delimiter
                <input value={csvDelimiter} onChange={(event) => setCsvDelimiter(event.target.value.slice(0, 1))} className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 font-mono font-normal text-slate-900" maxLength={1} required />
              </label>
              <label className="space-y-1.5 text-sm font-medium text-slate-700">
                CSV quote character
                <input value={csvQuotechar} onChange={(event) => setCsvQuotechar(event.target.value.slice(0, 1))} className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 font-mono font-normal text-slate-900" maxLength={1} required />
              </label>
            </>
          )}
        </div>
        <p className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          The converter retains source evidence for review. Its JSON output is not an FHWA submission file.
        </p>
      </div>
    </details>
  );

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <SectionHeader icon={<ArrowRightLeft className="w-6 h-6" />} title="Bridge Data Converter" />

      {/* Tabs */}
      <div className="flex gap-2">
        <button onClick={() => switchTab('nbi-to-snbi')} className={tabBtn(activeTab === 'nbi-to-snbi')}>
          <FileText className="w-4 h-4" /> NBI → SNBI
        </button>
        <button onClick={() => switchTab('snbi-to-nbi')} className={tabBtn(activeTab === 'snbi-to-nbi')}>
          <FileText className="w-4 h-4" /> SNBI → Review projections
        </button>
      </div>

      {/* ============ FORWARD ============ */}
      {activeTab === 'nbi-to-snbi' && (
        <>
          <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
            <div>
              <p className="text-base font-semibold text-slate-900">Convert NBI source files to SNBI JSON</p>
              <p className="mt-1 text-sm text-slate-500">Choose one year or a sequence of annual files, then upload the source. Standard NJDOT settings are already selected.</p>
            </div>
            <div className="grid grid-cols-2 gap-2 rounded-xl bg-slate-100 p-1">
              <button type="button" onClick={() => selectForwardMode('single')} className={`rounded-lg px-3 py-2 text-sm font-medium transition-colors ${forwardMode === 'single' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600 hover:text-slate-900'}`}>
                One inventory year
              </button>
              <button type="button" onClick={() => selectForwardMode('annual-history')} className={`rounded-lg px-3 py-2 text-sm font-medium transition-colors ${forwardMode === 'annual-history' ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600 hover:text-slate-900'}`}>
                Multiple yearly files
              </button>
            </div>
            {forwardMode === 'annual-history' && (
              <div className="rounded-xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900">
                Select consecutive annual files and confirm the inventory year for each one. BridgeGuard will reconstruct the inspection history for the target year.
              </div>
            )}
            {forwardMode === 'single' && (
              <>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <div className="space-y-2">
                <label className="block text-sm font-medium text-slate-700">NBI TXT file</label>
                <Dropzone id="txt-upload" accept=".txt,.csv" file={txtFile} label="Click to upload TXT"
                  onChange={(e) => e.target.files?.[0] && setTxtFile(e.target.files[0])} />
              </div>
              <div className="space-y-2">
                <label className="block text-sm font-medium text-slate-700">Element XML <span className="text-slate-400 font-normal">(optional)</span></label>
                <Dropzone id="xml-upload" accept=".xml" file={xmlFile} label="Click to upload XML"
                  onChange={(e) => e.target.files?.[0] && setXmlFile(e.target.files[0])} />
              </div>
            </div>
            {forwardAdvancedOptions}
            <button onClick={handleConvert} disabled={!txtFile || !sourceAgency.trim() || !bid01Policy || loading} className={`w-full ${primaryBtn}`}>
              {loading ? <Loader2 className="w-5 h-5 animate-spin" /> : <FileText className="w-5 h-5" />}
              {loading ? 'Processing…' : 'Convert to SNBI JSON'}
            </button>
              </>
            )}
            {forwardMode === 'annual-history' && (
              <div className="space-y-4">
                <label className="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed border-slate-300 px-6 py-7 text-center transition-colors hover:border-brand-400 hover:bg-slate-50">
                  <Upload className="h-9 w-9 text-slate-400" />
                  <span className="text-sm font-semibold text-slate-700">
                    {annualSources.length ? `${annualSources.length} annual files selected` : 'Select contiguous annual TXT or CSV files'}
                  </span>
                  <input type="file" accept=".txt,.csv" multiple onChange={handleAnnualFiles} className="hidden" />
                </label>
                {annualSources.length > 0 && (
                  <div className="overflow-hidden rounded-xl border border-slate-200">
                    <div className="grid grid-cols-[1fr_9rem] gap-3 bg-slate-50 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
                      <span>Source file</span><span>Inventory year</span>
                    </div>
                    {annualSources.map((item, index) => (
                      <div key={`${item.file.name}-${index}`} className="grid grid-cols-[1fr_9rem] items-center gap-3 border-t border-slate-200 px-3 py-2">
                        <span className="truncate text-sm text-slate-700" title={item.file.name}>{item.file.name}</span>
                        <input
                          type="number"
                          min={1900}
                          max={2199}
                          value={item.inventoryYear}
                          onChange={(event) => setAnnualSources((current) => current.map((entry, entryIndex) => entryIndex === index ? { ...entry, inventoryYear: event.target.value } : entry))}
                          className="rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900"
                          aria-label={`Inventory year for ${item.file.name}`}
                        />
                      </div>
                    ))}
                  </div>
                )}
                <label className="block space-y-1.5 text-sm font-medium text-slate-700">
                  Target inventory year to convert
                  <select value={targetInventoryYear} onChange={(event) => setTargetInventoryYear(event.target.value)} className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 font-normal text-slate-900">
                    <option value="" disabled>Select a year</option>
                    {[...new Set(annualYears)].sort((a, b) => a - b).map((year) => <option key={year} value={year}>{year}</option>)}
                  </select>
                </label>
                {annualHistoryProblem && annualSources.length > 0 && <p className="text-sm text-amber-700">{annualHistoryProblem}</p>}
                {forwardAdvancedOptions}
                <button onClick={handleHistoryConvert} disabled={Boolean(annualHistoryProblem) || !sourceAgency.trim() || !bid01Policy || loading} className={`w-full ${primaryBtn}`}>
                  {loading ? <Loader2 className="w-5 h-5 animate-spin" /> : <FileText className="w-5 h-5" />}
                  {loading ? 'Converting…' : 'Convert target year to SNBI JSON'}
                </button>
              </div>
            )}
          </div>

          {error && <ErrorCard msg={error} />}

          {result && (
            <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-3">
                  {result.status === 'blocked'
                    ? <AlertCircle className="w-6 h-6 text-amber-500 shrink-0" />
                    : <CheckCircle className="w-6 h-6 text-emerald-500 shrink-0" />}
                  <div>
                    <p className="font-semibold text-slate-800">Converted {result.converted} bridges{result.skipped > 0 ? ` · skipped ${result.skipped}` : ''}</p>
                    {result.elements_from_xml === false && (
                      <p className="text-xs text-amber-600">No Element XML — element fields left empty.</p>
                    )}
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  {result.diagnostics && (
                    <button onClick={handleDiagnosticsDownload} className={secondaryBtn}>
                      <Download className="w-4 h-4" /> Download diagnostics
                    </button>
                  )}
                  {downloadUrl && (
                    <button onClick={handleDownload} className={primaryBtn}>
                      <Download className="w-4 h-4" /> Download JSON
                    </button>
                  )}
                </div>
              </div>

              {result.inspection_history && (
                <div className={`rounded-xl border p-4 text-sm ${result.inspection_history.history_ready ? 'border-emerald-200 bg-emerald-50 text-emerald-900' : 'border-amber-200 bg-amber-50 text-amber-900'}`}>
                  <p className="font-semibold">Annual history {result.inspection_history.history_ready ? 'ready and attached' : 'requires correction'}</p>
                  <p className="mt-1">
                    {result.inspection_history.history_scope.start_inventory_year}â€“{result.inspection_history.history_scope.end_inventory_year} Â· target {result.inspection_history.history_scope.target_inventory_year} Â· {result.inspection_history.coverage.event_count} events ({result.inspection_history.coverage.deduplicated_event_count} deduplicated)
                  </p>
                  <p className="mt-1 text-xs">{result.inspection_history.history_scope.first_occurrence_semantics}. BIE03, BIE04, and BIE07 are not invented.</p>
                  {!result.inspection_history.history_ready && (
                    <ul className="mt-3 max-h-32 space-y-1 overflow-auto text-xs">
                      {result.inspection_history.bridges.flatMap((bridge) => bridge.issues).filter((issue) => issue.code.startsWith('insufficient_') || issue.code.includes('blocked') || issue.code.includes('missing') || issue.code.includes('errors')).slice(0, 20).map((issue, index) => (
                        <li key={`${issue.code}-${index}`}>{issue.code}: {issue.message}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {result.submission_label && (
                <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
                  <p className="font-semibold">{result.submission_label}</p>
                  {result.parser && (
                    <p className="mt-1 text-xs">
                      {result.parser.format} · {result.parser.encoding} · {result.parser.records_accepted} accepted · {result.parser.records_quarantined} quarantined
                    </p>
                  )}
                  {result.sources?.nbi?.sha256 && (
                    <p className="mt-1 break-all font-mono text-[11px] text-amber-800">Source SHA-256: {result.sources.nbi.sha256}</p>
                  )}
                </div>
              )}

              {result.parity_errors && result.parity_errors.length > 0 && (
                <div className="rounded-xl bg-amber-50 border border-amber-200 p-4">
                  <p className="text-sm font-medium text-amber-800 mb-1">Normalization warnings ({result.parity_errors.length})</p>
                  <ul className="text-xs text-amber-700 space-y-1 max-h-40 overflow-auto">
                    {result.parity_errors.slice(0, 25).map((msg, idx) => <li key={idx}>• {msg}</li>)}
                  </ul>
                </div>
              )}

              {result.validation?.summary && (
                <div className="rounded-xl bg-amber-50 border border-amber-200 p-4">
                  <p className="text-sm font-medium text-amber-800">{result.validation.validation_label || 'BridgeGuard validation'}</p>
                  <p className="text-sm text-amber-700 mt-1">
                    Critical: {result.validation.summary.critical} · Errors: {result.validation.summary.error} · Warnings: {result.validation.summary.warning} · Incomplete layers: {result.validation.summary.incomplete_layers}
                  </p>
                  {result.validation.disclaimer && <p className="mt-2 text-xs text-amber-800">{result.validation.disclaimer}</p>}
                  {(result.diagnostics?.issues?.length ?? 0) > 0 && (
                    <ul className="text-xs text-amber-700 mt-3 space-y-1 max-h-40 overflow-auto">
                      {(result.diagnostics?.issues ?? []).slice(0, 20).map((issue: any, idx: number) => (
                        <li key={idx}>{issue.stage}: {issue.code}{issue.json_pointer ? ` ${issue.json_pointer}` : ''} — {issue.message}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {result.coverage && (
                <div className="rounded-xl bg-slate-50 border border-slate-200 p-4 flex flex-wrap gap-6 text-sm">
                  <div>
                    <p className="text-slate-500 text-xs uppercase tracking-wider">Declared fields present</p>
                    <p className="font-semibold text-slate-800">{result.coverage.present_code_count} / {result.coverage.total_schema_codes}</p>
                  </div>
                  <div>
                    <p className="text-slate-500 text-xs uppercase tracking-wider">Declared fields absent</p>
                    <p className="font-semibold text-slate-800">{result.coverage.missing_code_count}</p>
                  </div>
                </div>
              )}

              {result.transition_coverage && (
                <div className="rounded-xl bg-slate-50 border border-slate-200 p-4">
                  <p className="text-sm font-medium text-slate-700">Transition evidence</p>
                  <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                    <div><p className="text-xs uppercase tracking-wider text-slate-500">Lineaged values</p><p className="font-semibold text-slate-800">{result.transition_coverage.lineage_leaf_count}/{result.transition_coverage.document_leaf_count}</p></div>
                    <div><p className="text-xs uppercase tracking-wider text-slate-500">Permanent</p><p className="font-semibold text-slate-800">{result.transition_coverage.populated_permanent_count}</p></div>
                    <div><p className="text-xs uppercase tracking-wider text-slate-500">Temporary</p><p className="font-semibold text-amber-700">{result.transition_coverage.populated_temporary_count}</p></div>
                    <div><p className="text-xs uppercase tracking-wider text-slate-500">Needs source/review</p><p className="font-semibold text-amber-700">{result.transition_coverage.null_source_count + result.transition_coverage.invalid_source_count + result.transition_coverage.no_transition_count}</p></div>
                  </div>
                  {result.eligibility && (
                    <p className="mt-3 text-xs text-slate-600">
                      Federal transition eligibility: {result.eligibility.federally_eligible_bridge_groups}/{result.eligibility.bridge_groups} bridge groups. Research retention is separate. Rule status: {result.eligibility.rule_status}.
                    </p>
                  )}
                  {result.bid01_preview && (
                    <p className="mt-1 text-xs text-slate-600">
                      Bridge ID policy: {result.bid01_preview.policy}; {result.bid01_preview.changed_count} of {result.bid01_preview.entry_count} IDs changed after collision preflight.
                    </p>
                  )}
                </div>
              )}

              {sectionCoverageEntries.length > 0 && (
                <div className="rounded-xl bg-slate-50 border border-slate-200 p-4">
                  <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
                    <p className="text-sm font-medium text-slate-700">Coverage by section</p>
                    <select
                      className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-800"
                      value={selectedCoverageSection}
                      onChange={(e) => setSelectedCoverageSection(e.target.value)}
                    >
                      {sectionCoverageEntries.map(([section]) => <option key={section} value={section}>{section}</option>)}
                    </select>
                  </div>
                  {selectedCoverageSection && result.section_coverage?.[selectedCoverageSection] && (
                    <div>
                      <p className="text-sm font-semibold text-slate-700">
                        {result.section_coverage[selectedCoverageSection].present_code_count}/{result.section_coverage[selectedCoverageSection].total_codes} present
                        <span className="font-normal text-slate-500"> · {result.section_coverage[selectedCoverageSection].missing_code_count} missing</span>
                      </p>
                      {result.section_coverage[selectedCoverageSection].missing_codes.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1.5 max-h-32 overflow-auto">
                          {result.section_coverage[selectedCoverageSection].missing_codes.map((code) => (
                            <span key={code} className="text-xs px-2 py-0.5 rounded bg-white border border-slate-200 text-slate-600 font-mono">{code}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              <div>
                <p className="text-sm font-medium text-slate-600 mb-2">Preview (first bridge)</p>
                <pre className="bg-slate-900 text-slate-100 p-4 rounded-xl overflow-auto max-h-80 text-xs font-mono">{JSON.stringify(result.bridges[0], null, 2)}</pre>
              </div>
            </div>
          )}
        </>
      )}

      {/* ============ REVERSE ============ */}
      {activeTab === 'snbi-to-nbi' && (
        <>
          <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
            <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              Generates review projections only. It does not restore the original NBI source or create an FHWA submission file.
            </div>
            <label className="block text-sm font-medium text-slate-700">SNBI JSON file</label>
            <Dropzone id="snbi-upload" accept=".json" file={snbiFile} label="Click to upload SNBI JSON" onChange={handleSnbiFileChange} />
            <button onClick={handleReverseConvert} disabled={!snbiFile || revLoading} className={`w-full ${primaryBtn}`}>
              {revLoading ? <Loader2 className="w-5 h-5 animate-spin" /> : <Download className="w-5 h-5" />}
              {revLoading ? 'Generating…' : 'Generate review projections'}
            </button>
          </div>

          {revError && <ErrorCard msg={revError} />}

          {revResult && (
            <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-3">
                  <CheckCircle className="w-6 h-6 text-emerald-500 shrink-0" />
                  <p className="font-semibold text-slate-800">Generated review projections for {revResult.count} bridge{revResult.count !== 1 ? 's' : ''}</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button onClick={() => downloadRev('csv')} className={primaryBtn}><Download className="w-4 h-4" /> Legacy projection CSV</button>
                  <button onClick={() => downloadRev('loss')} className={secondaryBtn}><Download className="w-4 h-4" /> Loss report JSON</button>
                  <button onClick={() => downloadRev('xml')} className="inline-flex items-center gap-2 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold py-3 px-6 transition-colors"><Download className="w-4 h-4" /> Elements projection XML</button>
                </div>
              </div>

              <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 space-y-1">
                <p className="font-semibold">Review-only generated output</p>
                <p>{revResult.legacy_projection.label}</p>
                <p>One synthetic record-type-1 row is generated per bridge. Repeated SNBI data may be collapsed or omitted; review the loss report before use.</p>
              </div>

              <div>
                <p className="text-sm font-medium text-slate-600 mb-2">Legacy-shaped headered CSV preview</p>
                <pre className="bg-slate-900 text-slate-100 p-4 rounded-xl overflow-auto max-h-48 text-xs font-mono">{revResult.nbi_csv.split('\n').slice(0, 6).join('\n')}</pre>
              </div>
              <div>
                <p className="text-sm font-medium text-slate-600 mb-1">Elements projection XML preview</p>
                <p className="text-xs text-slate-500 mb-2">Generated from the supplied SNBI elements for review; not an exact restoration of a source XML artifact.</p>
                <pre className="bg-slate-900 text-slate-100 p-4 rounded-xl overflow-auto max-h-48 text-xs font-mono">{revResult.elements_xml.slice(0, 800)}{revResult.elements_xml.length > 800 ? '\n…' : ''}</pre>
              </div>

              <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                <p className="text-sm font-medium text-slate-800 mb-2">Loss report summary</p>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(revResult.loss_manifest.classification_counts).map(([classification, count]) => (
                    <span key={classification} className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-700">
                      {classification.replace(/_/g, ' ')}: {count}
                    </span>
                  ))}
                </div>
              </div>

              {revResult.warnings && revResult.warnings.length > 0 && (
                <div className="rounded-xl bg-amber-50 border border-amber-200 p-4">
                  <p className="text-sm font-medium text-amber-800 mb-1">Schema warnings ({revResult.warnings.length})</p>
                  <ul className="text-xs text-amber-700 space-y-1 max-h-32 overflow-auto">
                    {revResult.warnings.map((w, i) => <li key={i}>• {w}</li>)}
                  </ul>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default Converter;
