// Access-token session state.
//
// The refresh token is an httpOnly cookie handled entirely by the browser;
// this module only persists the short-lived access token in localStorage so
// it can be attached to API requests. Storage is injectable so tests can run
// without a DOM.

export const ACCESS_TOKEN_KEY = 'lawyer_agent.access_token'
export const TENANT_TOKEN_KEY = 'lawyer_agent.tenant_access_token'

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
  }
}

export const session: Session = createSession()
