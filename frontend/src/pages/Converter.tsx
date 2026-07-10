// frontend/src/pages/Converter.tsx
import React, { useEffect, useMemo, useState } from 'react';
import axios from 'axios';
import { Upload, FileText, Download, AlertCircle, CheckCircle, ArrowRightLeft, Loader2 } from 'lucide-react';
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
}

interface ReverseConvertResponse {
  status: string;
  flat_json: any[];
  text: string;
  nbi_csv: string;
  elements_xml: string;
  count: number;
  warnings?: string[];
}

type TabMode = 'nbi-to-snbi' | 'snbi-to-nbi';

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
  const code = Number(bridges?.[0]?.BL01);
  return FIPS_STATE_NAMES[code] ?? (code ? String(code) : 'state');
}

const STORAGE_KEY_TAB = 'converter_active_tab';

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

  const handleConvert = async () => {
    if (!txtFile) { setError('Please upload an NBI TXT file'); return; }
    setLoading(true);
    setError(null);
    setResult(null);
    setDownloadUrl(null);
    const formData = new FormData();
    formData.append('txt_file', txtFile);
    if (xmlFile) formData.append('xml_file', xmlFile);
    try {
      const response = await axios.post<ConvertResponse>('/api/convert', formData, { headers: { 'Content-Type': 'multipart/form-data' } });
      setResult(response.data);
      const jsonBlob = new Blob([JSON.stringify(response.data.bridges, null, 2)], { type: 'application/json' });
      setDownloadUrl(URL.createObjectURL(jsonBlob));
    } catch (err: any) {
      setError(err.response?.data?.detail || err.response?.data?.error || (err.message ? `Request failed: ${err.message}` : 'Failed to convert files.'));
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
      setRevError(err.response?.data?.detail || err.response?.data?.error || (err.message ? `Request failed: ${err.message}` : 'Failed to reverse-convert file.'));
    } finally {
      setRevLoading(false);
    }
  };

  const downloadRev = (kind: 'txt' | 'xml') => {
    if (!revResult) return;
    const stateName = getStateName(revResult.flat_json);
    const date = new Date().toISOString().slice(0, 10);
    const blob = kind === 'txt'
      ? new Blob([revResult.nbi_csv], { type: 'text/plain' })
      : new Blob([revResult.elements_xml], { type: 'application/xml' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = kind === 'txt' ? `nbi_bridges_${stateName}_${date}.txt` : `elements_${stateName}_${date}.xml`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const primaryBtn = 'inline-flex items-center justify-center gap-2 rounded-xl bg-brand-600 hover:bg-brand-700 text-white font-semibold py-3 px-6 transition-colors disabled:opacity-50 disabled:cursor-not-allowed';
  const tabBtn = (active: boolean) =>
    `inline-flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-medium transition-colors ${
      active ? 'bg-brand-600 text-white shadow-sm' : 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'
    }`;

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <SectionHeader icon={<ArrowRightLeft className="w-6 h-6" />} title="Bridge Data Converter" />

      {/* Tabs */}
      <div className="flex gap-2">
        <button onClick={() => switchTab('nbi-to-snbi')} className={tabBtn(activeTab === 'nbi-to-snbi')}>
          <FileText className="w-4 h-4" /> NBI → SNBI
        </button>
        <button onClick={() => switchTab('snbi-to-nbi')} className={tabBtn(activeTab === 'snbi-to-nbi')}>
          <FileText className="w-4 h-4" /> SNBI → NBI + Elements
        </button>
      </div>

      {/* ============ FORWARD ============ */}
      {activeTab === 'nbi-to-snbi' && (
        <>
          <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
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
            <button onClick={handleConvert} disabled={!txtFile || loading} className={`w-full ${primaryBtn}`}>
              {loading ? <Loader2 className="w-5 h-5 animate-spin" /> : <FileText className="w-5 h-5" />}
              {loading ? 'Processing…' : 'Convert to SNBI JSON'}
            </button>
          </div>

          {error && <ErrorCard msg={error} />}

          {result && (
            <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-3">
                  <CheckCircle className="w-6 h-6 text-emerald-500 shrink-0" />
                  <div>
                    <p className="font-semibold text-slate-800">Converted {result.converted} bridges{result.skipped > 0 ? ` · skipped ${result.skipped}` : ''}</p>
                    {result.elements_from_xml === false && (
                      <p className="text-xs text-amber-600">No Element XML — element fields left empty.</p>
                    )}
                  </div>
                </div>
                <button onClick={handleDownload} className={primaryBtn}>
                  <Download className="w-4 h-4" /> Download JSON
                </button>
              </div>

              {result.parity_errors && result.parity_errors.length > 0 && (
                <div className="rounded-xl bg-amber-50 border border-amber-200 p-4">
                  <p className="text-sm font-medium text-amber-800 mb-1">Schema parity warnings ({result.parity_errors.length})</p>
                  <ul className="text-xs text-amber-700 space-y-1 max-h-40 overflow-auto">
                    {result.parity_errors.slice(0, 25).map((msg, idx) => <li key={idx}>• {msg}</li>)}
                  </ul>
                </div>
              )}

              {result.validation?.summary && (
                <div className="rounded-xl bg-amber-50 border border-amber-200 p-4">
                  <p className="text-sm font-medium text-amber-800">Validation</p>
                  <p className="text-sm text-amber-700 mt-1">
                    Critical: {result.validation.summary.critical} · Errors: {result.validation.summary.error} · Flags: {result.validation.summary.flag} · Safety: {result.validation.summary.safety}
                  </p>
                  {result.validation.issues?.length > 0 && (
                    <ul className="text-xs text-amber-700 mt-3 space-y-1 max-h-40 overflow-auto">
                      {result.validation.issues.slice(0, 20).map((issue: any, idx: number) => (
                        <li key={idx}>{issue.type}: {issue.bridge} {issue.tag} — {issue.message}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {result.coverage && (
                <div className="rounded-xl bg-slate-50 border border-slate-200 p-4 flex flex-wrap gap-6 text-sm">
                  <div>
                    <p className="text-slate-500 text-xs uppercase tracking-wider">Schema codes present</p>
                    <p className="font-semibold text-slate-800">{result.coverage.present_code_count} / {result.coverage.total_schema_codes}</p>
                  </div>
                  <div>
                    <p className="text-slate-500 text-xs uppercase tracking-wider">Missing</p>
                    <p className="font-semibold text-slate-800">{result.coverage.missing_code_count}</p>
                  </div>
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
            <label className="block text-sm font-medium text-slate-700">SNBI JSON file</label>
            <Dropzone id="snbi-upload" accept=".json" file={snbiFile} label="Click to upload SNBI JSON" onChange={handleSnbiFileChange} />
            <button onClick={handleReverseConvert} disabled={!snbiFile || revLoading} className={`w-full ${primaryBtn}`}>
              {revLoading ? <Loader2 className="w-5 h-5 animate-spin" /> : <Download className="w-5 h-5" />}
              {revLoading ? 'Processing…' : 'Convert to NBI + Elements'}
            </button>
          </div>

          {revError && <ErrorCard msg={revError} />}

          {revResult && (
            <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-3">
                  <CheckCircle className="w-6 h-6 text-emerald-500 shrink-0" />
                  <p className="font-semibold text-slate-800">Converted {revResult.count} bridge{revResult.count !== 1 ? 's' : ''}</p>
                </div>
                <div className="flex gap-2">
                  <button onClick={() => downloadRev('txt')} className={primaryBtn}><Download className="w-4 h-4" /> TXT</button>
                  <button onClick={() => downloadRev('xml')} className="inline-flex items-center gap-2 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold py-3 px-6 transition-colors"><Download className="w-4 h-4" /> XML</button>
                </div>
              </div>

              <div>
                <p className="text-sm font-medium text-slate-600 mb-2">NBI TXT preview</p>
                <pre className="bg-slate-900 text-slate-100 p-4 rounded-xl overflow-auto max-h-48 text-xs font-mono">{revResult.nbi_csv.split('\n').slice(0, 6).join('\n')}</pre>
              </div>
              <div>
                <p className="text-sm font-medium text-slate-600 mb-2">Elements XML preview</p>
                <pre className="bg-slate-900 text-slate-100 p-4 rounded-xl overflow-auto max-h-48 text-xs font-mono">{revResult.elements_xml.slice(0, 800)}{revResult.elements_xml.length > 800 ? '\n…' : ''}</pre>
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
