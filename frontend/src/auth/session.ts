// Access-token session state.
//
// The refresh token is an httpOnly cookie handled entirely by the browser;
// this module only persists the short-lived access token in localStorage so
// it can be attached to API requests. Storage is injectable so tests can run
// without a DOM.

export const ACCESS_TOKEN_KEY = 'lawyer_agent.access_token'
export const TENANT_TOKEN_KEY = 'lawyer_agent.tenant_access_token'
export const ACTIVE_TENANT_KEY = 'lawyer_agent.active_tenant'
export interface ActiveTenant { tenant_id: string; membership_id: string; name?: string }

export function tokenClaims(token: string | null): Record<string, unknown> | null {
  try { return JSON.parse(atob((token ?? '').split('.')[1]!.replaceAll('-', '+').replaceAll('_', '/'))) as Record<string, unknown> } catch { return null }
}

export interface TokenStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export interface Session {
  readToken(): string | null
  saveToken(token: string): void
  clearToken(): void
  isAuthenticated(): boolean
  readTenantToken(): string | null
  saveTenantToken(token: string): void
  clearTenantToken(): void
  readActiveTenant(): ActiveTenant | null
  saveActiveTenant(tenant: ActiveTenant): void
  clearActiveTenant(): void
  accountIdentity(): string | null
  tenantIdentity(): string | null
}

function browserStorage(): TokenStorage | null {
  if (typeof window === 'undefined' || typeof window.localStorage === 'undefined') {
    return null
  }
  return window.localStorage
}

export function createSession(storage: TokenStorage | null = browserStorage()): Session {
  return {
    readToken(): string | null {
      if (!storage) return null
      return storage.getItem(ACCESS_TOKEN_KEY)
    },
    saveToken(token: string): void {
      if (!storage) return
      storage.setItem(ACCESS_TOKEN_KEY, token)
    },
    clearToken(): void {
      if (!storage) return
      storage.removeItem(ACCESS_TOKEN_KEY)
    },
    isAuthenticated(): boolean {
      const token = this.readToken()
      return typeof token === 'string' && token.length > 0
    },
    readTenantToken(): string | null {
      if (!storage) return null
      return storage.getItem(TENANT_TOKEN_KEY)
    },
    saveTenantToken(token: string): void {
      if (!storage) return
      storage.setItem(TENANT_TOKEN_KEY, token)
    },
    clearTenantToken(): void {
      if (!storage) return
      storage.removeItem(TENANT_TOKEN_KEY)
    },
    readActiveTenant() {
      try {
        const value = JSON.parse(storage?.getItem(ACTIVE_TENANT_KEY) ?? 'null') as ActiveTenant | null
        return value && typeof value.tenant_id === 'string' && typeof value.membership_id === 'string' ? value : null
      } catch { return null }
    },
    saveActiveTenant(tenant) { storage?.setItem(ACTIVE_TENANT_KEY, JSON.stringify(tenant)) },
    clearActiveTenant() { storage?.removeItem(ACTIVE_TENANT_KEY) },
    accountIdentity() {
      const token = this.readToken()
      const claims = tokenClaims(token)
      return claims ? `${String(claims.sub)}:${String(claims.sid)}:${String(claims.auth_version)}` : token
    },
    tenantIdentity() {
      const account = this.accountIdentity()
      const tenant = this.readActiveTenant()
      return account && tenant ? `${account}:${tenant.tenant_id}:${tenant.membership_id}` : null
    },
  }
}

export const session: Session = createSession()
