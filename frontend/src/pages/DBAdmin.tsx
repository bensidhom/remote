// frontend/src/pages/DBAdmin.tsx

import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import {
  Shield,
  Table2,
  Plus,
  Trash2,
  Save,
  X,
  ChevronLeft,
  ChevronRight,
  Terminal,
  Play,
  Columns3,
  AlertCircle,
  CheckCircle2,
  Pencil,
} from 'lucide-react';

interface ColumnSchema {
  cid: number;
  name: string;
  type: string;
  notnull: boolean;
  default: any;
  pk: boolean;
}

interface TableRow {
  rowid: number;
  [key: string]: any;
}

const DBAdmin: React.FC = () => {
  // ── PIN Gate ──
  // ── State ──
  const [tables, setTables] = useState<string[]>([]);
  const [selectedTable, setSelectedTable] = useState('');
  const [schema, setSchema] = useState<ColumnSchema[]>([]);
  const [rows, setRows] = useState<TableRow[]>([]);
  const [columns, setColumns] = useState<string[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [pageSize] = useState(50);
  const [loading, setLoading] = useState(false);

  // Editing
  const [editingRow, setEditingRow] = useState<number | null>(null);
  const [editBuffer, setEditBuffer] = useState<Record<string, any>>({});
  const [addingRow, setAddingRow] = useState(false);
  const [newRowBuffer, setNewRowBuffer] = useState<Record<string, any>>({});

  // SQL runner
  const [sqlQuery, setSqlQuery] = useState('');
  const [sqlResult, setSqlResult] = useState<{ columns: string[]; rows: Record<string, any>[]; count: number } | null>(null);
  const [sqlError, setSqlError] = useState('');
  const [sqlRunning, setSqlRunning] = useState(false);

  // Notifications
  const [toast, setToast] = useState<{ type: 'success' | 'error'; message: string } | null>(null);

  // ── Tab ──
  const [activeTab, setActiveTab] = useState<'browser' | 'sql'>('browser');

  // ── Delete all data ──
  const [deletingAll, setDeletingAll] = useState(false);

  const handleDeleteAllData = async () => {
    if (!window.confirm('Delete ALL data from every table? This cannot be undone.')) return;
    if (!window.confirm('Are you absolutely sure? All bridges and related records will be permanently deleted.')) return;
    setDeletingAll(true);
    try {
      await axios.delete('/api/clear-data');
      showToast('success', 'All data deleted successfully');
      loadRows();
    } catch (e: any) {
      showToast('error', e.response?.data?.detail || 'Failed to delete data');
    } finally {
      setDeletingAll(false);
    }
  };

  const showToast = useCallback((type: 'success' | 'error', message: string) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 3000);
  }, []);

  // ── Load tables ──
  useEffect(() => {
    axios.get('/api/tables').then(r => {
      setTables(r.data);
      if (r.data.length > 0) setSelectedTable(r.data[0]);
    });
  }, []);

  // ── Load schema + rows when table changes ──
  useEffect(() => {
    if (!selectedTable) return;
    setPage(0);
    setEditingRow(null);
    setAddingRow(false);
    loadSchema();
  }, [selectedTable]);

  useEffect(() => {
    if (!selectedTable) return;
    loadRows();
  }, [selectedTable, page]);

  const loadSchema = async () => {
    try {
      const r = await axios.get(`/api/table-schema/${selectedTable}`);
      setSchema(r.data);
    } catch {
      setSchema([]);
    }
  };

  const loadRows = async () => {
    setLoading(true);
    try {
      const r = await axios.get(`/api/table-rows/${selectedTable}?limit=${pageSize}&offset=${page * pageSize}`);
      setRows(r.data.rows);
      setColumns(r.data.columns);
      setTotal(r.data.total);
    } catch {
      setRows([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  };

  // ── CRUD ──
  const startEdit = (row: TableRow) => {
    setEditingRow(row.rowid);
    const buf: Record<string, any> = {};
    columns.filter(c => c !== 'rowid').forEach(c => (buf[c] = row[c]));
    setEditBuffer(buf);
  };

  const cancelEdit = () => {
    setEditingRow(null);
    setEditBuffer({});
  };

  const saveEdit = async () => {
    if (editingRow === null) return;
    if (!window.confirm('Save changes to this row?')) return;
    try {
      await axios.put(`/api/table/${selectedTable}/row/${editingRow}`, editBuffer);
      showToast('success', 'Row updated');
      setEditingRow(null);
      loadRows();
    } catch (e: any) {
      showToast('error', e.response?.data?.detail || 'Update failed');
    }
  };

  const startAddRow = () => {
    setAddingRow(true);
    const buf: Record<string, any> = {};
    schema.forEach(c => (buf[c.name] = c.default ?? ''));
    setNewRowBuffer(buf);
  };

  const saveNewRow = async () => {
    if (!window.confirm('Insert this new row?')) return;
    try {
      // Strip empty strings → null
      const payload: Record<string, any> = {};
      for (const [k, v] of Object.entries(newRowBuffer)) {
        payload[k] = v === '' ? null : v;
      }
      await axios.post(`/api/table/${selectedTable}/row`, payload);
      showToast('success', 'Row inserted');
      setAddingRow(false);
      loadRows();
    } catch (e: any) {
      showToast('error', e.response?.data?.detail || 'Insert failed');
    }
  };

  const deleteRow = async (rowid: number) => {
    if (!window.confirm('Delete this row? This cannot be undone.')) return;
    try {
      await axios.delete(`/api/table/${selectedTable}/row/${rowid}`);
      showToast('success', 'Row deleted');
      loadRows();
    } catch (e: any) {
      showToast('error', e.response?.data?.detail || 'Delete failed');
    }
  };

  // ── SQL Runner ──
  const runSql = async () => {
    if (!sqlQuery.trim()) return;
    setSqlRunning(true);
    setSqlError('');
    setSqlResult(null);
    try {
      const r = await axios.post('/api/sql-query', { sql: sqlQuery });
      setSqlResult(r.data);
    } catch (e: any) {
      setSqlError(e.response?.data?.detail || 'Query failed');
    } finally {
      setSqlRunning(false);
    }
  };

  const totalPages = Math.ceil(total / pageSize);
  const editableCols = columns.filter(c => c !== 'rowid');

  // ── PIN Lock Screen ──
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-4xl font-bold text-gray-900 dark:text-white flex items-center gap-4">
            <Shield className="w-10 h-10 text-orange-600" />
            Database Admin
          </h1>
          <p className="text-gray-600 dark:text-gray-400 mt-2">
            Browse, edit, and query your SNBI SQLite database
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleDeleteAllData}
            disabled={deletingAll}
            className="flex items-center gap-1.5 px-4 py-2 text-xs rounded-xl bg-red-600 hover:bg-red-700 disabled:bg-gray-400 text-white transition-colors"
            title="Delete all data from the database"
          >
            <Trash2 className="w-3.5 h-3.5" /> {deletingAll ? 'Deleting...' : 'Delete All Data'}
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-2">
        <button
          onClick={() => setActiveTab('browser')}
          className={`flex items-center gap-2 px-5 py-2.5 rounded-2xl font-medium text-sm transition-all ${
            activeTab === 'browser'
              ? 'bg-orange-600 text-white shadow-lg'
              : 'glass text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-750'
          }`}
        >
          <Table2 className="w-4 h-4" /> Table Browser
        </button>
        <button
          onClick={() => setActiveTab('sql')}
          className={`flex items-center gap-2 px-5 py-2.5 rounded-2xl font-medium text-sm transition-all ${
            activeTab === 'sql'
              ? 'bg-orange-600 text-white shadow-lg'
              : 'glass text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-750'
          }`}
        >
          <Terminal className="w-4 h-4" /> SQL Query
        </button>
      </div>

      {/* ────── TABLE BROWSER TAB ────── */}
      {activeTab === 'browser' && (
        <div className="flex gap-6">
          {/* Left: Table list + schema */}
          <div className="w-64 flex-shrink-0 space-y-4">
            {/* Table list */}
            <div className="glass rounded-3xl border border-gray-200 dark:border-gray-700 shadow-sm overflow-hidden">
              <div className="px-5 py-4 border-b border-gray-200 dark:border-gray-700">
                <span className="font-semibold text-gray-900 dark:text-white text-sm">Tables</span>
                <span className="ml-2 text-xs text-gray-500">({tables.length})</span>
              </div>
              <div className="max-h-48 overflow-y-auto">
                {tables.map(t => (
                  <button
                    key={t}
                    onClick={() => setSelectedTable(t)}
                    className={`w-full text-left px-5 py-2.5 text-sm font-mono transition-colors ${
                      t === selectedTable
                        ? 'bg-orange-50 dark:bg-orange-900/20 text-orange-700 dark:text-orange-400 font-semibold'
                        : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-750'
                    }`}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </div>

            {/* Schema */}
            {schema.length > 0 && (
              <div className="glass rounded-3xl border border-gray-200 dark:border-gray-700 shadow-sm overflow-hidden">
                <div className="px-5 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center gap-2">
                  <Columns3 className="w-4 h-4 text-gray-500" />
                  <span className="font-semibold text-gray-900 dark:text-white text-sm">Schema</span>
                </div>
                <div className="max-h-80 overflow-y-auto">
                  {schema.map(col => (
                    <div key={col.cid} className="px-5 py-2 border-b border-gray-100 dark:border-gray-700/50 last:border-0">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs text-gray-900 dark:text-white">{col.name}</span>
                        {col.pk && <span className="text-[10px] px-1.5 py-0.5 bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400 rounded-full font-medium">PK</span>}
                        {col.notnull && <span className="text-[10px] px-1.5 py-0.5 bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400 rounded-full font-medium">NN</span>}
                      </div>
                      <span className="text-[11px] text-gray-500 dark:text-gray-400 font-mono">{col.type || 'ANY'}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Right: Data grid */}
          <div className="flex-1 min-w-0">
            <div className="glass rounded-3xl border border-gray-200 dark:border-gray-700 shadow-sm overflow-hidden">
              {/* Toolbar */}
              <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className="font-semibold text-gray-900 dark:text-white">{selectedTable}</span>
                  <span className="text-sm text-gray-500">{total} rows</span>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={startAddRow}
                    disabled={addingRow}
                    className="flex items-center gap-1.5 px-4 py-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-gray-400 text-white text-sm rounded-xl font-medium transition-colors"
                  >
                    <Plus className="w-4 h-4" /> Add Row
                  </button>
                  <button
                    onClick={loadRows}
                    className="flex items-center gap-1.5 px-4 py-2 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200 text-sm rounded-xl font-medium transition-colors"
                  >
                    Refresh
                  </button>
                </div>
              </div>

              {/* Table */}
              <div className="overflow-x-auto max-h-[60vh] overflow-y-auto">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 z-10">
                    <tr className="bg-gray-50 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700">
                      <th className="px-4 py-3 text-left font-medium text-gray-500 text-xs w-20">Actions</th>
                      {editableCols.map(col => (
                        <th key={col} className="px-4 py-3 text-left font-medium text-gray-600 dark:text-gray-400 whitespace-nowrap text-xs font-mono">
                          {col}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-700/50">
                    {/* New row form */}
                    {addingRow && (
                      <tr className="bg-emerald-50 dark:bg-emerald-900/10">
                        <td className="px-4 py-2">
                          <div className="flex gap-1">
                            <button onClick={saveNewRow} className="p-1 text-emerald-600 hover:text-emerald-800" title="Save">
                              <Save className="w-4 h-4" />
                            </button>
                            <button onClick={() => setAddingRow(false)} className="p-1 text-gray-400 hover:text-gray-600" title="Cancel">
                              <X className="w-4 h-4" />
                            </button>
                          </div>
                        </td>
                        {editableCols.map(col => (
                          <td key={col} className="px-4 py-1">
                            <input
                              type="text"
                              value={newRowBuffer[col] ?? ''}
                              onChange={e => setNewRowBuffer(prev => ({ ...prev, [col]: e.target.value }))}
                              className="w-full min-w-[80px] glass border border-gray-300 dark:border-gray-600 rounded-lg px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-emerald-500 text-gray-900 dark:text-white"
                              placeholder={col}
                            />
                          </td>
                        ))}
                      </tr>
                    )}

                    {/* Data rows */}
                    {loading ? (
                      <tr><td colSpan={editableCols.length + 1} className="px-6 py-12 text-center text-gray-500">Loading...</td></tr>
                    ) : rows.length === 0 ? (
                      <tr><td colSpan={editableCols.length + 1} className="px-6 py-12 text-center text-gray-500">No data</td></tr>
                    ) : (
                      rows.map(row => (
                        <tr key={row.rowid} className="hover:bg-gray-50 dark:hover:bg-gray-800/50 group">
                          <td className="px-4 py-2">
                            {editingRow === row.rowid ? (
                              <div className="flex gap-1">
                                <button onClick={saveEdit} className="p-1 text-blue-600 hover:text-blue-800" title="Save">
                                  <Save className="w-4 h-4" />
                                </button>
                                <button onClick={cancelEdit} className="p-1 text-gray-400 hover:text-gray-600" title="Cancel">
                                  <X className="w-4 h-4" />
                                </button>
                              </div>
                            ) : (
                              <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                <button onClick={() => startEdit(row)} className="p-1 text-gray-400 hover:text-blue-600" title="Edit">
                                  <Pencil className="w-3.5 h-3.5" />
                                </button>
                                <button onClick={() => deleteRow(row.rowid)} className="p-1 text-gray-400 hover:text-red-600" title="Delete">
                                  <Trash2 className="w-3.5 h-3.5" />
                                </button>
                              </div>
                            )}
                          </td>
                          {editableCols.map(col => (
                            <td key={col} className="px-4 py-2">
                              {editingRow === row.rowid ? (
                                <input
                                  type="text"
                                  value={editBuffer[col] ?? ''}
                                  onChange={e => setEditBuffer(prev => ({ ...prev, [col]: e.target.value }))}
                                  className="w-full min-w-[80px] glass border border-gray-300 dark:border-gray-600 rounded-lg px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-500 text-gray-900 dark:text-white"
                                />
                              ) : (
                                <span className="text-xs font-mono text-gray-700 dark:text-gray-300 break-all">
                                  {row[col] === null || row[col] === undefined ? <span className="text-gray-400 italic">NULL</span> : String(row[col])}
                                </span>
                              )}
                            </td>
                          ))}
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>

              {/* Pagination */}
              {totalPages > 1 && (
                <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
                  <span className="text-sm text-gray-500">
                    Page {page + 1} of {totalPages}
                  </span>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setPage(p => Math.max(0, p - 1))}
                      disabled={page === 0}
                      className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-xl bg-gray-200 dark:bg-gray-700 disabled:opacity-40 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200 transition-colors"
                    >
                      <ChevronLeft className="w-4 h-4" /> Prev
                    </button>
                    <button
                      onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
                      disabled={page >= totalPages - 1}
                      className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-xl bg-gray-200 dark:bg-gray-700 disabled:opacity-40 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200 transition-colors"
                    >
                      Next <ChevronRight className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ────── SQL TAB ────── */}
      {activeTab === 'sql' && (
        <div className="space-y-4">
          <div className="glass rounded-3xl border border-gray-200 dark:border-gray-700 shadow-sm overflow-hidden">
            <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
              <span className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
                <Terminal className="w-5 h-5 text-orange-500" /> SQL Query Runner
              </span>
              <span className="text-xs text-gray-500">Read-only: SELECT, PRAGMA, EXPLAIN</span>
            </div>
            <div className="p-6">
              <textarea
                value={sqlQuery}
                onChange={e => setSqlQuery(e.target.value)}
                placeholder="SELECT * FROM bridges LIMIT 10;"
                rows={5}
                className="w-full bg-gray-50 dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded-2xl p-4 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-orange-500 text-gray-900 dark:text-white placeholder-gray-400 resize-y"
                onKeyDown={e => {
                  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                    e.preventDefault();
                    runSql();
                  }
                }}
              />
              <div className="flex items-center gap-4 mt-4">
                <button
                  onClick={runSql}
                  disabled={sqlRunning || !sqlQuery.trim()}
                  className="flex items-center gap-2 px-6 py-2.5 bg-orange-600 hover:bg-orange-700 disabled:bg-gray-400 text-white rounded-xl font-medium text-sm transition-colors"
                >
                  <Play className="w-4 h-4" />
                  {sqlRunning ? 'Running...' : 'Run Query'}
                </button>
                <span className="text-xs text-gray-500">Ctrl+Enter to run</span>
              </div>
            </div>
          </div>

          {/* SQL Error */}
          {sqlError && (
            <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-2xl p-4 flex items-start gap-3">
              <AlertCircle className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
              <span className="text-sm text-red-700 dark:text-red-400 font-mono">{sqlError}</span>
            </div>
          )}

          {/* SQL Results */}
          {sqlResult && (
            <div className="glass rounded-3xl border border-gray-200 dark:border-gray-700 shadow-sm overflow-hidden">
              <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="w-5 h-5 text-emerald-500" />
                  <span className="text-sm text-gray-700 dark:text-gray-300">{sqlResult.count} rows returned</span>
                </div>
                <button
                  onClick={() => { setSqlResult(null); setSqlError(''); }}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-xl bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 text-gray-600 dark:text-gray-300 transition-colors"
                >
                  <X className="w-3.5 h-3.5" /> Clear
                </button>
              </div>
              <div className="overflow-x-auto max-h-[50vh] overflow-y-auto">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 z-10">
                    <tr className="bg-gray-50 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700">
                      {sqlResult.columns.map(col => (
                        <th key={col} className="px-4 py-3 text-left font-medium text-gray-600 dark:text-gray-400 whitespace-nowrap text-xs font-mono">
                          {col}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-700/50">
                    {sqlResult.rows.map((row, idx) => (
                      <tr key={idx} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                        {sqlResult.columns.map(col => (
                          <td key={col} className="px-4 py-2 text-xs font-mono text-gray-700 dark:text-gray-300 break-all">
                            {row[col] === null || row[col] === undefined ? <span className="text-gray-400 italic">NULL</span> : String(row[col])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div className={`fixed bottom-8 right-8 z-50 flex items-center gap-3 px-5 py-3 rounded-2xl shadow-2xl text-sm font-medium transition-all ${
          toast.type === 'success'
            ? 'bg-emerald-600 text-white'
            : 'bg-red-600 text-white'
        }`}>
          {toast.type === 'success' ? <CheckCircle2 className="w-5 h-5" /> : <AlertCircle className="w-5 h-5" />}
          {toast.message}
        </div>
      )}
    </div>
  );
};

export default DBAdmin;
