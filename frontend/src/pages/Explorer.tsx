// frontend/src/pages/Explorer.tsx

import React, { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { Database, Filter, Search, Play } from 'lucide-react';
import { SectionHeader } from '../components/ui';
import { useDataGenerationEpoch } from '../lib/dataGeneration';

interface TableData {
  [key: string]: any;
}

interface StateOption {
  state_code: number;
  state_name: string;
}

const Explorer: React.FC = () => {
  const [tables, setTables] = useState<string[]>([]);
  const [selectedTable, setSelectedTable] = useState<string>('');
  const [selectedState, setSelectedState] = useState<string>('All States');
  const [availableStates, setAvailableStates] = useState<StateOption[]>([]);
  const [tableData, setTableData] = useState<TableData[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [rowsLimit, setRowsLimit] = useState<number>(100);
  const [totalRows, setTotalRows] = useState<number>(0);
  const dataGenerationEpoch = useDataGenerationEpoch();
  const previousGenerationEpochRef = useRef(dataGenerationEpoch);
  const hasLoadedDataRef = useRef(false);
  const tableRequestRef = useRef<AbortController | null>(null);

  // Fetch list of tables and available states on component mount
  useEffect(() => {
    const controller = new AbortController();
    const fetchTables = async () => {
      try {
        const response = await axios.get<string[]>('/api/tables', { signal: controller.signal });
        const nextTables = Array.isArray(response.data) ? response.data : [];
        setTables(nextTables);
        setSelectedTable((current) => nextTables.includes(current) ? current : (nextTables[0] || ''));
      } catch (err) {
        if (controller.signal.aborted) return;
        console.error(err);
        setError('Failed to load database tables');
      }
    };
    const fetchStates = async () => {
      try {
        const response = await axios.get('/api/states', { signal: controller.signal });
        setAvailableStates(response.data);
      } catch (err) {
        if (controller.signal.aborted) return;
        console.error(err);
      }
    };
    void fetchTables();
    void fetchStates();
    return () => controller.abort();
  }, [dataGenerationEpoch]);

  // Manual load function
  const loadTableData = useCallback(async () => {
    if (!selectedTable) return;

    tableRequestRef.current?.abort();
    const controller = new AbortController();
    tableRequestRef.current = controller;
    hasLoadedDataRef.current = true;
    setLoading(true);
    setError(null);

    try {
      let url = `/api/table/${selectedTable}?limit=${rowsLimit}`;
      if (selectedState !== 'All States') {
        url += `&state_code=${selectedState}`;
      }
      const response = await axios.get(url, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setTableData(response.data);
      setTotalRows(response.data.length);
    } catch (err) {
      if (controller.signal.aborted) return;
      console.error(err);
      setError('Failed to load table data');
      setTableData([]);
      setTotalRows(0);
    } finally {
      if (tableRequestRef.current === controller) {
        tableRequestRef.current = null;
        setLoading(false);
      }
    }
  }, [rowsLimit, selectedState, selectedTable]);

  useEffect(() => () => tableRequestRef.current?.abort(), []);

  // If the user had loaded a table, refresh that same bounded view after a
  // publish. Otherwise discard any derived local search results and stay idle.
  useEffect(() => {
    if (previousGenerationEpochRef.current === dataGenerationEpoch) return;
    previousGenerationEpochRef.current = dataGenerationEpoch;
    setSearchTerm('');
    setTableData([]);
    setTotalRows(0);
    if (hasLoadedDataRef.current && selectedTable) void loadTableData();
  }, [dataGenerationEpoch, loadTableData, selectedTable]);

  // Filter data based on search term
  const filteredData = tableData.filter(row => 
    Object.values(row).some(value => 
      String(value).toLowerCase().includes(searchTerm.toLowerCase())
    )
  );

  return (
    <div className="space-y-8">
      <SectionHeader
        icon={<Database className="w-6 h-6" />}
        title="SNBI Database Explorer"
        subtitle="Browse and inspect all tables in your local SNBI database"
      />

      {/* Controls */}
      <div className="flex flex-wrap gap-4 glass p-6 rounded-3xl shadow-sm border border-gray-200 dark:border-gray-700">
        {/* Table Selector */}
        <div className="flex-1 min-w-[200px]">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            Select Table
          </label>
          <select
            value={selectedTable}
            onChange={(e) => setSelectedTable(e.target.value)}
            className="w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded-2xl px-4 py-3 focus:outline-none focus:ring-2 focus:ring-indigo-500 text-gray-900 dark:text-white"
          >
            {tables.map(table => (
              <option key={table} value={table}>
                {table}
              </option>
            ))}
          </select>
        </div>

        {/* State Filter */}
        <div className="flex-1 min-w-[200px]">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            Filter by State
          </label>
          <select
            value={selectedState}
            onChange={(e) => setSelectedState(e.target.value)}
            className="w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded-2xl px-4 py-3 focus:outline-none focus:ring-2 focus:ring-indigo-500 text-gray-900 dark:text-white"
          >
            <option value="All States">All States</option>
            {availableStates.map((s) => (
              <option key={s.state_code} value={String(s.state_code)}>
                {s.state_name}
              </option>
            ))}
          </select>
        </div>

        {/* Rows Limit */}
        <div className="flex-1 min-w-[150px]">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            Rows to Load
          </label>
          <select
            value={rowsLimit}
            onChange={(e) => setRowsLimit(Number(e.target.value))}
            className="w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded-2xl px-4 py-3 focus:outline-none focus:ring-2 focus:ring-indigo-500 text-gray-900 dark:text-white"
          >
            <option value={50}>50 rows</option>
            <option value={100}>100 rows</option>
            <option value={250}>250 rows</option>
            <option value={500}>500 rows</option>
            <option value={1000}>1000 rows</option>
            <option value={0}>All rows</option>
          </select>
        </div>

        {/* Load Button */}
        <div className="flex items-end">
          <button
            onClick={loadTableData}
            disabled={loading || !selectedTable}
            className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-gray-400 text-white px-8 py-3 rounded-2xl font-medium transition-colors"
          >
            <Play className="w-4 h-4" />
            {loading ? 'Loading...' : 'Load Data'}
          </button>
        </div>
      </div>

      {/* Results Area */}
      <div className="glass rounded-3xl shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <div className="px-8 py-5 border-b border-gray-200 dark:border-gray-700 flex justify-between items-center">
          <div>
            <span className="font-semibold text-lg text-gray-900 dark:text-white">{selectedTable || 'No table selected'}</span>
            <span className="ml-3 text-sm text-gray-500 dark:text-gray-400">
              {filteredData.length} of {totalRows} rows loaded
            </span>
            {rowsLimit > 0 && totalRows >= rowsLimit && (
              <span className="ml-2 text-xs text-amber-600 dark:text-amber-400">
                (Limited to {rowsLimit} rows)
              </span>
            )}
          </div>
          {loading && <div className="text-sm text-gray-500 dark:text-gray-400">Loading...</div>}
        </div>

        {/* Search in loaded data */}
        {tableData.length > 0 && (
          <div className="px-8 py-4 border-b border-gray-200 dark:border-gray-700">
            <div className="flex items-center gap-4">
              <div className="relative flex-1 max-w-md">
                <Search className="absolute left-4 top-3.5 w-5 h-5 text-gray-400" />
                <input
                  type="text"
                  placeholder="Search in loaded data..."
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  className="w-full bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-700 rounded-2xl pl-12 py-3 focus:outline-none focus:ring-2 focus:ring-indigo-500 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400"
                />
              </div>
              <div className="text-sm text-gray-500 dark:text-gray-400">
                {filteredData.length !== totalRows ? `${filteredData.length} matching rows` : `${totalRows} total rows`}
              </div>
            </div>
          </div>
        )}

        {error && (
          <div className="p-8 text-red-600 text-center">{error}</div>
        )}

        {!error && tableData.length === 0 && !loading && selectedTable && (
          <div className="p-16 text-center text-gray-500 dark:text-gray-400">
            <Database className="w-12 h-12 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
            <p className="text-lg font-medium mb-2">Ready to explore</p>
            <p>Click "Load Data" to view records from the {selectedTable} table</p>
          </div>
        )}

        {!selectedTable && !loading && (
          <div className="p-16 text-center text-gray-500 dark:text-gray-400">
            <Database className="w-12 h-12 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
            <p className="text-lg font-medium mb-2">Select a table to begin</p>
            <p>Choose a table from the dropdown above to start exploring your data</p>
          </div>
        )}

        {tableData.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700">
                  {tableData[0] && Object.keys(tableData[0]).map((column) => (
                    <th 
                      key={column}
                      className="px-6 py-4 text-left font-medium text-gray-600 dark:text-gray-400 whitespace-nowrap"
                    >
                      {column}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                {filteredData.map((row, index) => (
                  <tr key={index} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                    {Object.values(row).map((value, i) => (
                      <td 
                        key={i} 
                        className="px-6 py-4 text-gray-700 dark:text-gray-300 font-mono text-xs break-all"
                      >
                        {value === null || value === undefined ? '—' : String(value)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

export default Explorer;
