import axios from 'axios';

export type UserRole = 'viewer' | 'editor' | 'admin';

export interface AuthUser {
  email: string;
  role: UserRole;
  workspace_id: string;
}

/** Restore the HttpOnly server session, if one exists. */
export async function getCurrentUser(): Promise<AuthUser | null> {
  try {
    const response = await axios.get<AuthUser>('/api/auth/me');
    return response.data;
  } catch (error: any) {
    if (error?.response?.status === 401) return null;
    throw error;
  }
}

const PRIVATE_STORAGE_PREFIX = 'bg:private:v1:';

export function privateStorageNamespace(userEmail: string, workspaceId: string): string {
  const owner = encodeURIComponent(userEmail.trim().toLowerCase());
  const workspace = encodeURIComponent(workspaceId.trim());
  return `${PRIVATE_STORAGE_PREFIX}${workspace}:${owner}:`;
}

export function clearPrivateBrowserState(userEmail?: string, workspaceId?: string): void {
  const exactPrefix = userEmail && workspaceId
    ? privateStorageNamespace(userEmail, workspaceId)
    : PRIVATE_STORAGE_PREFIX;
  for (let index = localStorage.length - 1; index >= 0; index -= 1) {
    const key = localStorage.key(index);
    if (key?.startsWith(exactPrefix)) localStorage.removeItem(key);
  }
  // Shared keys from pre-namespacing builds are unsafe on shared workstations.
  ['vision_indexed_session', 'vision_indexed_doc_hash', 'vision_indexed_doc_name', 'vision_chat_history']
    .forEach((key) => localStorage.removeItem(key));
}

export async function signOut(userEmail?: string, workspaceId?: string): Promise<string | null> {
  let logoutUrl: string | null = null;
  try {
    const response = await axios.post<{ logout_url?: string | null }>('/api/auth/logout');
    logoutUrl = response.data.logout_url || null;
  } finally {
    // Remove obsolete credentials created by pre-server-auth builds.
    localStorage.removeItem('bg-user-email');
    localStorage.removeItem('dbadmin_pin');
    localStorage.removeItem('admin_session');
    clearPrivateBrowserState(userEmail, workspaceId);
  }
  return logoutUrl;
}

// Retained for the optional license acknowledgement screen.
export function acceptLicense(): void {
  localStorage.setItem('bg-license-ack', '1');
}
