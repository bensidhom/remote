import React, { createContext, useContext } from 'react';
import type { UserRole } from '../lib/access';

interface AdminContextType {
  isAdmin: boolean;
  canEdit: boolean;
  role: UserRole;
}

const AdminContext = createContext<AdminContextType>({
  isAdmin: false,
  canEdit: false,
  role: 'viewer',
});

export const AdminProvider: React.FC<{ children: React.ReactNode; role: UserRole }> = ({ children, role }) => (
  <AdminContext.Provider value={{
    role,
    isAdmin: role === 'admin',
    canEdit: role === 'admin' || role === 'editor',
  }}>
    {children}
  </AdminContext.Provider>
);

export const useAdmin = () => useContext(AdminContext);

