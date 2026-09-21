import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { api, getToken, onUnauthorized, setToken } from '../lib/api.js'
import { getStoredAuth, clearAuth } from '../lib/oidc.js'

const SiteNameContext = createContext(null)

export function useSiteName() {
  return useContext(SiteNameContext)
}

// Claims carried by the validated access token (decoded client-side for
// DISPLAY only -- the middleware and agent service are the security
// boundary; they verify the signature). Entra-style claims: roles[], site_id.
const AuthContext = createContext(null)

export function useAuth() {
  return useContext(AuthContext)
}

export function AuthProvider({ children }) {
  // Token state comes from the OIDC flow (lib/oidc.js writes localStorage);
  // the api layer (lib/api.js) reads the same token for Authorization headers.
  const [session, setSession] = useState(() => getStoredAuth())

  useEffect(() => {
    // The api layer calls this on any 401: token expiry becomes a clean
    // redirect to login instead of a silent failure or a blank screen.
    onUnauthorized(() => {
      clearAuth()
      setSession(null)
    })
  }, [])

  const completeLogin = useCallback(() => {
    setSession(getStoredAuth())
  }, [])

  const logout = useCallback(() => {
    clearAuth()
    setSession(null)
  }, [])

  const value = useMemo(() => {
    const claims = session?.claims || {}
    const role = Array.isArray(claims.roles) ? claims.roles[0] : claims.roles || null
    return {
      token: session?.token || null,
      user: claims.preferred_username || claims.name || claims.sub || null,
      role,
      siteId: claims.site_id || null,
      siteName: claims.site_name || null,
      // MaintenanceEngineer approves up to HITL level 2; PlantManager up to 3.
      maxApprovalLevel:
        role === 'PlantManager' ? 3 : role === 'MaintenanceEngineer' ? 2 : 0,
      completeLogin,
      logout,
    }
  }, [session, completeLogin, logout])

  return (
    <SiteNameContext.Provider value={value.siteName || 'Site 12 — Paper Mill Cooling Tower'}>
      <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
    </SiteNameContext.Provider>
  )
}
