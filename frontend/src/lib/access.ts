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

/** Authenticate on the server. No password is stored in the browser bundle. */
export async function signIn(email: string, password: string): Promise<AuthUser> {
  const response = await axios.post<AuthUser>('/api/auth/login', { email, password });
  return response.data;
}

export async function signOut(): Promise<void> {
  try {
    await axios.post('/api/auth/logout');
  } finally {
    // Remove obsolete credentials created by pre-server-auth builds.
    localStorage.removeItem('bg-user-email');
    localStorage.removeItem('dbadmin_pin');
    localStorage.removeItem('admin_session');
  }
}

// Retained for the optional license acknowledgement screen.
export function acceptLicense(): void {
  localStorage.setItem('bg-license-ack', '1');
}
