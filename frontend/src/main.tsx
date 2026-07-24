// frontend/src/main.tsx
import React from 'react'
import ReactDOM from 'react-dom/client'
import '@fontsource-variable/inter'
import Landing from './pages/Landing'
import './index.css'

const path = window.location.pathname.replace(/\/+$/, '') || '/'
const isWorkspace = path === '/app' || path.startsWith('/app/')
const WorkspaceApp = React.lazy(() => import('./App'))

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {isWorkspace ? (
      <React.Suspense fallback={<div className="workspace-loading">Opening BridgeGuard…</div>}>
        <WorkspaceApp />
      </React.Suspense>
    ) : <Landing />}
  </React.StrictMode>,
)
