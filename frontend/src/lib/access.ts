import axios from 'axios';

export type UserRole = 'viewer' | 'editor' | 'admin';

export interface AuthUser {
  email: string;
  role: UserRole;
}

export interface AuthConfig {
  mode: 'password' | 'cloudflare' | 'disabled';
}

/** Read the server's authentication mode without exposing any auth secrets. */
export async function getAuthConfig(): Promise<AuthConfig> {
  const response = await axios.get<AuthConfig>('/api/auth/config');
  return response.data;
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

/** Authenticate on the server. No password is stored in the browser bundle. */
export async function signIn(email: string, password: string): Promise<AuthUser> {
  const response = await axios.post<AuthUser>('/api/auth/login', { email, password });
  return response.data;
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
