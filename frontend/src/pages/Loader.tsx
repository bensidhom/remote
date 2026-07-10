// frontend/src/pages/Loader.tsx
// Load an SNBI JSON snapshot (one per bridge per data year) and manage loaded
// years. Inspection-report / PDF extraction lives per bridge now, not here.
import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { CheckCircle, AlertCircle, FileJson, Database, Loader2 } from 'lucide-react';
import { SectionHeader } from '../components/ui';

const MIN_DATA_YEAR = 1990;
const MAX_DATA_YEAR = 2100;
const CURRENT_YEAR = new Date().getFullYear();

const Loader: React.FC = () => {
  const [jsonFile, setJsonFile] = useState<File | null>(null);
  const [selectedDataYear, setSelectedDataYear] = useState<number>(CURRENT_YEAR);
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [loadedCount, setLoadedCount] = useState(0);
  const [validation, setValidation] = useState<any | null>(null);
  const [skipInfo, setSkipInfo] = useState<{ count: number; reasons: string[] } | null>(null);
  const [loadedYears, setLoadedYears] = useState<number[]>([]);
  const [deleting, setDeleting] = useState(false);

  const fetchLoadedYears = async () => {
    try {
      const r = await axios.get('/api/loaded-years');
      setLoadedYears((r.data?.years || []).slice().sort((a: number, b: number) => b - a));
    } catch { /* ignore */ }
  };

  useEffect(() => { fetchLoadedYears(); }, []);

  const deleteYear = async (year: number) => {
    if (!window.confirm(`Delete all New Jersey bridges loaded for ${year}? This cannot be undone.`)) return;
    setDeleting(true);
    setError('');
    try {
      const r = await axios.delete(`/api/bridges/by-year?state_code=34&data_year=${year}`);
      setSuccess(true);
      setMessage(`Deleted ${r.data?.deleted ?? 0} bridge(s) for data year ${year}.`);
      localStorage.setItem('db_last_updated', Date.now().toString());
      fetchLoadedYears();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to delete bridges for that year.');
    } finally {
      setDeleting(false);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.type === 'application/json' || file.name.endsWith('.json')) {
      setJsonFile(file);
      setSuccess(false);
      setError('');
      setMessage('');
    } else {
      setError('Please upload a valid JSON file');
    }
  };

  const handleLoad = async () => {
    if (!jsonFile) { setError('Please select a JSON file first'); return; }
    if (!Number.isInteger(selectedDataYear) || selectedDataYear < MIN_DATA_YEAR || selectedDataYear > MAX_DATA_YEAR) {
      setError(`Please choose a data year between ${MIN_DATA_YEAR} and ${MAX_DATA_YEAR}`);
      return;
    }
    setLoading(true);
    setError('');
    setSuccess(false);
    setMessage('');
    setValidation(null);
    setSkipInfo(null);

    const formData = new FormData();
    formData.append('file', jsonFile);
    formData.append('data_year', String(selectedDataYear));

    try {
      const response = await axios.post('/api/load-json', formData, { headers: { 'Content-Type': 'multipart/form-data' } });
      if (response.data.status === 'conflict') {
        setError(response.data.message || `Data year ${selectedDataYear} is already loaded. Delete it first, then reload.`);
      } else {
        setLoadedCount((response.data.loaded ?? 0) + (response.data.already_existed ?? 0));
        setSuccess(true);
        setMessage(response.data.message || `Loaded ${response.data.loaded} bridge snapshots for ${selectedDataYear}.`);
        setValidation(response.data.validation || null);
        setSkipInfo(response.data.skipped ? { count: response.data.skipped, reasons: response.data.skipped_reasons || [] } : null);
        localStorage.setItem('db_last_updated', Date.now().toString());
        fetchLoadedYears();
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load JSON into database. Please check the file format.');
    } finally {
      setLoading(false);
    }
  };

  const resetForm = () => {
    setJsonFile(null);
    setSuccess(false);
    setError('');
    setMessage('');
    setLoadedCount(0);
    setValidation(null);
    setSkipInfo(null);
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <SectionHeader icon={<Database className="w-6 h-6" />} title="Load / Delete Bridges" />

      {/* Upload */}
      <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6 space-y-5">
        <label className="flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed border-slate-300 hover:border-brand-400 hover:bg-slate-50 transition-colors cursor-pointer py-10 px-6 text-center">
          <FileJson className="w-10 h-10 text-slate-400" />
          <span className="text-sm font-semibold text-slate-700">
            {jsonFile ? jsonFile.name : 'Click to upload SNBI JSON file'}
          </span>
          <span className="text-xs text-slate-400">Files produced by the Converter</span>
          <input type="file" accept=".json" onChange={handleFileChange} className="hidden" />
        </label>

        <div className="flex flex-col sm:flex-row sm:items-end gap-4">
          <div className="sm:w-48">
            <label htmlFor="data-year" className="block text-sm font-medium text-slate-700 mb-1.5">Data year</label>
            <input
              id="data-year"
              type="number"
              min={MIN_DATA_YEAR}
              max={MAX_DATA_YEAR}
              value={selectedDataYear}
              onChange={(e) => setSelectedDataYear(Number(e.target.value))}
              className="w-full rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500"
            />
          </div>
          <button
            onClick={handleLoad}
            disabled={!jsonFile || loading || selectedDataYear < MIN_DATA_YEAR || selectedDataYear > MAX_DATA_YEAR}
            className="flex-1 inline-flex items-center justify-center gap-2 rounded-xl bg-brand-600 hover:bg-brand-700 text-white font-semibold py-2.5 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? <Loader2 className="w-5 h-5 animate-spin" /> : <Database className="w-5 h-5" />}
            {loading ? 'Loading…' : 'Load into database'}
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-2xl bg-red-50 border border-red-200 p-4 flex gap-3">
          <AlertCircle className="w-5 h-5 text-red-600 mt-0.5 shrink-0" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {/* Success */}
      {success && (
        <div className="rounded-2xl bg-white border border-slate-200 shadow-sm p-5 space-y-4">
          <div className="flex items-center gap-3">
            <CheckCircle className="w-6 h-6 text-emerald-500 shrink-0" />
            <div>
              <p className="font-semibold text-slate-800">{message}</p>
              {loadedCount > 0 && <p className="text-sm text-slate-500">{loadedCount} snapshot(s) for data year {selectedDataYear}.</p>}
            </div>
            <button onClick={resetForm} className="ml-auto text-sm rounded-lg px-3 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-700">Load another</button>
          </div>

          {skipInfo && skipInfo.count > 0 && (
            <div className="rounded-xl bg-orange-50 border border-orange-200 p-4">
              <p className="text-sm font-medium text-orange-800">{skipInfo.count} record(s) skipped (not loaded)</p>
              {skipInfo.reasons.length > 0 && (
                <ul className="text-xs text-orange-700 mt-2 space-y-1 max-h-36 overflow-auto font-mono">
                  {skipInfo.reasons.map((reason, idx) => <li key={idx}>• {reason}</li>)}
                </ul>
              )}
            </div>
          )}

          {validation?.summary && (
            <div className="rounded-xl bg-amber-50 border border-amber-200 p-4">
              <p className="text-sm font-medium text-amber-800">Validation</p>
              <p className="text-sm text-amber-700 mt-1">
                Critical: {validation.summary.critical} · Errors: {validation.summary.error} · Flags: {validation.summary.flag} · Safety: {validation.summary.safety}
              </p>
              {validation.issues?.length > 0 && (
                <ul className="text-xs text-amber-700 mt-3 space-y-1 max-h-36 overflow-auto">
                  {validation.issues.slice(0, 15).map((issue: any, idx: number) => (
                    <li key={idx}>{issue.type}: {issue.bridge} {issue.tag} — {issue.message}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}

      {/* Loaded years */}
      {loadedYears.length > 0 && (
        <div className="rounded-3xl bg-white border border-slate-200 shadow-sm p-6">
          <p className="text-sm font-semibold text-slate-700 mb-3">Loaded data years</p>
          <div className="flex flex-wrap gap-2">
            {loadedYears.map((y) => (
              <span key={y} className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 pl-3 pr-1.5 py-1.5 text-sm">
                <span className="font-medium text-slate-700">{y}</span>
                <button
                  onClick={() => deleteYear(y)}
                  disabled={deleting}
                  title={`Delete all bridges for ${y}`}
                  className="grid place-items-center w-6 h-6 rounded-lg text-red-500 hover:bg-red-500/15 transition-colors disabled:opacity-50"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
          <p className="text-xs text-slate-400 mt-3">Delete a year before re-loading it.</p>
        </div>
      )}
    </div>
  );
};

export default Loader;
