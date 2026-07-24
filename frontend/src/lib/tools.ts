// Central registry + event bus for the map-first shell. Every tool surface
// (TopNav, landing cards, map chips, bridge popups) opens tools through one
// `openTool()` call so there is a single source of truth for labels/icons and
// a single listener in App.tsx.
import {
  FileText, Upload, Database, BarChart3, BrainCircuit,
  Edit3, Shield, Camera, Bot, Box, type LucideIcon,
} from 'lucide-react';

export type ToolId =
  | 'map' | 'converter' | 'loader' | 'explorer' | 'editor'
  | 'kpis' | 'forecasts' | 'dbadmin' | 'vision'
  | 'bridgette' | 'bridge' | 'pointcloud';

export interface ToolMeta {
  id: ToolId;
  label: string;
  desc: string;
  Icon: LucideIcon;
  /** Accent color (hex) for chips/cards. */
  accent: string;
  /** Whether the tool is gated behind admin mode. */
  admin?: boolean;
}

export const TOOLS: ToolMeta[] = [
  { id: 'converter',  label: 'SNBI Converter', desc: 'SNBI ⇄ NBI conversion',     Icon: FileText,    accent: '#3b82f6' },
  { id: 'loader',     label: 'Load / Delete',  desc: 'Load & delete bridges by year', Icon: Upload,  accent: '#10b981' },
  { id: 'kpis',       label: 'KPIs',           desc: 'New Jersey, by county',      Icon: BarChart3,   accent: '#f59e0b' },
  { id: 'bridgette',  label: 'Bridgette',      desc: 'Ask the bridge database',    Icon: Bot,         accent: '#8b5cf6' },
  // Reachable from the per-bridge popup or the admin lock — not on the sidebar.
  { id: 'explorer',   label: 'Explorer',       desc: 'Query & browse records',     Icon: Database,    accent: '#8b5cf6' },
  { id: 'editor',     label: 'Editor',         desc: 'Edit bridge records',        Icon: Edit3,       accent: '#06b6d4' },
  { id: 'forecasts',  label: 'Forecasts',      desc: 'Condition predictions',      Icon: BrainCircuit, accent: '#6366f1' },
  { id: 'vision',     label: 'Vision',         desc: 'AI image analysis',          Icon: Camera,      accent: '#14b8a6' },
  { id: 'pointcloud', label: 'Point Cloud',    desc: '3D bridge scan analysis',    Icon: Box,         accent: '#0ea5e9' },
  { id: 'dbadmin',    label: 'DB Admin',       desc: 'Database management',        Icon: Shield,      accent: '#f97316', admin: true },
];

/** Tools shown on the home sidebar (in order). */
export const SIDEBAR_TOOLS: ToolId[] = ['converter', 'loader', 'kpis', 'bridgette'];

export interface ToolPayload {
  stateCode?: number | string;
  bridgeNumber?: number | string;
  bridgeName?: string;
}

const OPEN_EVENT = 'bg:open-tool';

/** Open a tool modal from anywhere in the app. */
export function openTool(tool: ToolId, payload?: ToolPayload): void {
  window.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: { tool, payload } }));
}

/** Subscribe to open-tool requests. Returns an unsubscribe fn. */
export function onOpenTool(handler: (tool: ToolId, payload?: ToolPayload) => void): () => void {
  const fn = (e: Event) => {
    const detail = (e as CustomEvent).detail as { tool: ToolId; payload?: ToolPayload };
    handler(detail.tool, detail.payload);
  };
  window.addEventListener(OPEN_EVENT, fn);
  // Back-compat with the source branch's bespoke events.
  const vision = () => handler('vision');
  const dbadmin = () => handler('dbadmin');
  window.addEventListener('open-vision-modal', vision);
  window.addEventListener('open-db-admin-modal', dbadmin);
  return () => {
    window.removeEventListener(OPEN_EVENT, fn);
    window.removeEventListener('open-vision-modal', vision);
    window.removeEventListener('open-db-admin-modal', dbadmin);
  };
}
