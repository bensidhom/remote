// frontend/src/App.tsx
// Map-first shell: the ArcGIS map landing (Home) is always mounted as the
// backdrop; every tool opens as a floating modal via the openTool() event bus.
import React, { useState, useEffect, useCallback } from 'react';
import { BrowserRouter as Router } from 'react-router-dom';
import axios from 'axios';
import { Ban, X } from 'lucide-react';
import SettingsDialog from './components/SettingsDialog';
import { AdminProvider } from './context/AdminContext';
import { Modal } from './components/ui';
import { onOpenTool, type ToolId, type ToolPayload } from './lib/tools';
import { getCurrentUser, signOut, type AuthUser } from './lib/access';

import HomeSidebar from './components/HomeSidebar';
import MapPage from './pages/Map';

const Bridgette = React.lazy(() => import('./pages/Bridgette'));
const Converter = React.lazy(() => import('./pages/Converter'));
const Loader = React.lazy(() => import('./pages/Loader'));
const Explorer = React.lazy(() => import('./pages/Explorer'));
const Vision = React.lazy(() => import('./pages/Vision'));
const Kpis = React.lazy(() => import('./pages/Kpis'));
const Editor = React.lazy(() => import('./pages/Editor'));
const DBAdmin = React.lazy(() => import('./pages/DBAdmin'));
const Forecasts = React.lazy(() => import('./pages/Forecasts'));
const PointCloud = React.lazy(() => import('./pages/PointCloud'));

type ModalSize = 'md' | 'lg' | 'xl' | 'full';
const TOOL_SIZE: Partial<Record<ToolId, ModalSize>> = {
  map: 'full',
  dbadmin: 'full',
  editor: 'full',
  kpis: 'xl',
  forecasts: 'xl',
  bridgette: 'xl',
  converter: 'lg',
  loader: 'lg',
  explorer: 'lg',
  pointcloud: 'xl',
};

const AppContent: React.FC<{ user: AuthUser; onSignOut: () => void }> = ({ user, onSignOut }) => {
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Light-only professional theme. Dark mode was retired in favour of a clean
  // light-grey look; the `dark:` utility classes stay harmless (never applied).
  const darkMode = false;

  // Which tool modal is open (vision is handled separately so RAG survives).
  const [activeTool, setActiveTool] = useState<Exclude<ToolId, 'vision'> | null>(null);
  // Tools the user has opened this session — kept mounted (hidden) so switching
  // between them or closing back to the map preserves their state.
  const [openedTools, setOpenedTools] = useState<Exclude<ToolId, 'vision'>[]>([]);
  const [toolPayload, setToolPayload] = useState<ToolPayload | undefined>(undefined);
  const [visionOpen, setVisionOpen] = useState(false);
  const [visionMounted, setVisionMounted] = useState(false);

  // Global Vision RAG processing state.
  const [visionRagActive, setVisionRagActive] = useState(false);
  const [visionRagStage, setVisionRagStage] = useState('');
  const [visionRagEta, setVisionRagEta] = useState<number | null>(null);

  // Tool launcher subscription.
  useEffect(() => {
    return onOpenTool((tool, payload) => {
      if (tool === 'dbadmin' && user.role !== 'admin') return;
      if (tool === 'vision') { setVisionMounted(true); setVisionOpen(true); return; }
      setToolPayload(payload);
      setOpenedTools((prev) => (prev.includes(tool) ? prev : [...prev, tool]));
      setActiveTool(tool);
    });
  }, [user.role]);

  // Close all tool modals (brand click) → back to the map landing.
  useEffect(() => {
    const close = () => { setActiveTool(null); setVisionOpen(false); };
    window.addEventListener('bg:close-tools', close);
    return () => window.removeEventListener('bg:close-tools', close);
  }, []);

  // Poll Vision RAG progress.
  useEffect(() => {
    if (!visionRagActive) { setVisionRagStage(''); setVisionRagEta(null); return; }
    const id = setInterval(async () => {
      try {
        const r = await axios.get('/api/vision-progress');
        if (r.data.stage) setVisionRagStage(r.data.stage);
        setVisionRagEta(typeof r.data.eta_seconds === 'number' ? r.data.eta_seconds : null);
      } catch { /* ignore */ }
    }, 2000);
    return () => clearInterval(id);
  }, [visionRagActive]);

  const handleGlobalCancel = useCallback(async () => {
    try { await axios.post('/api/vision-cancel'); } catch { /* ignore */ }
    setVisionRagActive(false); setVisionRagStage(''); setVisionRagEta(null);
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode);
    localStorage.setItem('theme', darkMode ? 'dark' : 'light');
  }, [darkMode]);

  const renderTool = (tool: Exclude<ToolId, 'vision'>) => {
    switch (tool) {
      case 'map': return <MapPage />;
      case 'converter': return <Converter />;
      case 'loader': return <Loader />;
      case 'explorer': return <Explorer />;
      case 'editor': return <Editor />;
      case 'kpis': return <Kpis />;
      case 'bridgette': return <Bridgette />;
      case 'forecasts': return <Forecasts />;
      case 'dbadmin': return <DBAdmin />;
      case 'pointcloud': return (
        <PointCloud
          bridgeStateCode={toolPayload?.stateCode}
          bridgeNumber={toolPayload?.bridgeNumber != null ? String(toolPayload.bridgeNumber) : undefined}
          bridgeName={toolPayload?.bridgeName}
        />
      );
      default: return null;
    }
  };

  // The large map is NOT kept mounted while hidden — it re-inits on open.
  // Everything else preserves state across switch/close.
  const EPHEMERAL: Exclude<ToolId, 'vision'>[] = ['map', 'pointcloud'];
  const isFullBleed = (tool: ToolId | null) => tool === 'map' || tool === 'pointcloud';

  return (
    <div className="flex flex-col h-screen bg-slate-100">
      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />

      {/* Home = sidebar (primary nav) + the working county-drilldown map landing */}
      <div className="flex flex-1 overflow-hidden">
       <HomeSidebar userEmail={user.email} onSignOut={onSignOut} />
       <main className="flex-1 overflow-hidden relative">
        <div className="absolute inset-0 p-3 sm:p-4">
          <div className="h-full rounded-3xl overflow-hidden border border-slate-200 dark:border-slate-800 shadow-2xl glass">
            <MapPage />
          </div>
        </div>

        {/* Global Vision RAG processing banner */}
        {visionRagActive && !visionOpen && (
          <div className="absolute top-4 left-1/2 -translate-x-1/2 z-[1200] flex items-center gap-3 glass-strong border border-amber-300/60 dark:border-amber-700/60 rounded-xl px-4 py-2.5 text-sm shadow-lg">
            <div className="w-2.5 h-2.5 rounded-full bg-amber-500 animate-pulse shrink-0" />
            <span className="text-amber-700 dark:text-amber-300 font-medium">Vision RAG processing</span>
            {visionRagStage && <span className="text-amber-600 dark:text-amber-400 truncate max-w-xs">— {visionRagStage}</span>}
            {visionRagEta !== null && visionRagEta > 0 && (
              <span className="text-amber-500 font-mono text-xs ml-1 shrink-0">
                ~{visionRagEta < 60 ? `${Math.ceil(visionRagEta)}s` : `${Math.floor(visionRagEta / 60)}m ${Math.ceil(visionRagEta % 60)}s`} left
              </span>
            )}
            <button onClick={handleGlobalCancel} className="ml-auto flex items-center gap-1.5 px-3 py-1 bg-red-500 hover:bg-red-600 text-white rounded-lg text-xs font-medium transition-colors shrink-0">
              <Ban className="w-3.5 h-3.5" /> Cancel
            </button>
          </div>
        )}
       </main>
      </div>

      {/* Tool modals (everything except Vision; each page renders its own header).
          Opened tools stay mounted (hidden) so switching/closing preserves their
          state; heavy viewers in EPHEMERAL only render while active. */}
      {openedTools.map((tool) => (
        <Modal
          key={tool}
          keepMounted
          open={tool === activeTool}
          onClose={() => setActiveTool(null)}
          size={TOOL_SIZE[tool]}
          padded={!isFullBleed(tool)}
        >
          <React.Suspense fallback={<div className="p-8 text-center text-slate-500">Loading tool…</div>}>
            {(!EPHEMERAL.includes(tool) || tool === activeTool) ? renderTool(tool) : null}
          </React.Suspense>
        </Modal>
      ))}

      {/* Vision — kept mounted so long-running RAG isn't cancelled on close */}
      {visionMounted && <div
        className={visionOpen ? 'fixed inset-0 z-[4600] flex items-start justify-center p-4 sm:p-8 overflow-y-auto' : ''}
        style={visionOpen ? { background: 'rgba(2,6,23,0.55)', backdropFilter: 'blur(6px)' } : { display: 'none' }}
        onMouseDown={(e) => { if (e.target === e.currentTarget) setVisionOpen(false); }}
      >
        <div className="relative w-full max-w-6xl my-auto glass-strong rounded-3xl shadow-2xl border border-slate-200/80 dark:border-slate-700/80 max-h-[calc(100vh-4rem)] overflow-hidden flex flex-col">
          <button
            onClick={() => setVisionOpen(false)}
            aria-label="Close Vision"
            className="absolute top-3.5 right-3.5 z-10 grid place-items-center w-9 h-9 rounded-full bg-slate-100/80 text-slate-500 hover:bg-red-500 hover:text-white dark:bg-slate-800/80 dark:text-slate-300 transition-colors"
          >
            <X className="w-4 h-4" />
          </button>
          <div className="flex-1 overflow-auto ui-scroll p-6">
            <React.Suspense fallback={<div className="p-8 text-center text-slate-500">Loading Vision…</div>}>
              <Vision
                onRagActiveChange={setVisionRagActive}
              />
            </React.Suspense>
          </div>
        </div>
      </div>}
    </div>
  );
};

const App: React.FC = () => {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);

  useEffect(() => {
    let active = true;
    getCurrentUser()
      .then((current) => { if (active) setUser(current); })
      .catch(() => { if (active) setUser(null); })
      .finally(() => { if (active) setCheckingSession(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const interceptor = axios.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error?.response?.status === 401) {
          setUser(null);
        }
        return Promise.reject(error);
      },
    );
    return () => axios.interceptors.response.eject(interceptor);
  }, []);

  if (checkingSession) {
    return <div className="fixed inset-0 grid place-items-center bg-slate-950 text-slate-300">Checking session…</div>;
  }

  if (!user) {
    return (
      <main className="workspace-access-gate">
        <section className="workspace-access-card" aria-labelledby="workspace-access-title">
          <img src="/bridgeguard.png" alt="BridgeGuard" width="1024" height="1024" />
          <div className="workspace-access-copy">
            <p className="workspace-access-eyebrow">BridgeGuard secure workspace</p>
            <h1 id="workspace-access-title">Sign in to continue</h1>
            <p>Use an approved account to open bridge data, maps, and inspection tools.</p>
          </div>
          <a className="workspace-access-action" href="/app?signin=1">
            Continue with email code
          </a>
          <p className="workspace-access-note">
            Cloudflare emails a one-time code to approved accounts.
          </p>
          <a className="workspace-access-gate__back" href="/">Back to public page</a>
        </section>
      </main>
    );
  }

  return (
    <Router>
      <AdminProvider role={user.role}>
        <AppContent user={user} onSignOut={() => {
          void signOut().then((logoutUrl) => {
            setUser(null);
            if (logoutUrl) window.location.replace(logoutUrl);
          });
        }} />
      </AdminProvider>
    </Router>
  );
};

export default App;
