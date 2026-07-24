import axios from 'axios';

export type UserRole = 'viewer' | 'editor' | 'admin';

export interface AuthUser {
  email: string;
  role: UserRole;
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

export async function signOut(): Promise<string | null> {
  let logoutUrl: string | null = null;
  try {
    const response = await axios.post<{ logout_url?: string | null }>('/api/auth/logout');
    logoutUrl = response.data.logout_url || null;
  } finally {
    // Remove obsolete credentials created by pre-server-auth builds.
    localStorage.removeItem('bg-user-email');
    localStorage.removeItem('dbadmin_pin');
    localStorage.removeItem('admin_session');
  }
  return logoutUrl;
}

// Retained for the optional license acknowledgement screen.
export function acceptLicense(): void {
  localStorage.setItem('bg-license-ack', '1');
}
