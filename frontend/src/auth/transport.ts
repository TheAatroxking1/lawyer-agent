import { ApiError, type FetchLike } from '../api/client'
import { session, tokenClaims, type Session, type ActiveTenant } from './session'
import { refreshAuth } from './state'

type Scope = 'account' | 'tenant'
interface Options {
  session: Session
  fetchImpl?: FetchLike
  baseUrl?: string
  csrfProvider?: () => string | null
  onChange?: () => void
  lock?: <T>(action: () => Promise<T>) => Promise<T>
}
function csrfCookie(): string | null {
  if (typeof document === 'undefined') return null
  const value = document.cookie.split('; ').find(value => value.startsWith('__Host-lawyer_csrf='))
  return value ? decodeURIComponent(value.slice(value.indexOf('=') + 1)) : null
}
async function browserLock<T>(action: () => Promise<T>): Promise<T> {
  return typeof navigator !== 'undefined' && navigator.locks
    ? navigator.locks.request('lawyer_agent.refresh', action) : action()
}
function expired(token: string | null): boolean {
  if (!token) return true
  const claims = tokenClaims(token)
  return typeof claims?.exp === 'number' && claims.exp * 1000 <= Date.now() + 15_000
}
export function createAuthTransport(options: Options) {
  const state = options.session
  const raw: FetchLike = options.fetchImpl ?? ((url, init) => fetch(url, init))
  const base = options.baseUrl ?? '/api/v1'
  const changed = options.onChange ?? (() => {})
  const csrf = options.csrfProvider ?? csrfCookie
  const lock = options.lock ?? browserLock
  let refreshing: Promise<void> | null = null
  let selectionGeneration = 0
  function clear() { selectionGeneration += 1; state.clearToken(); state.clearTenantToken(); state.clearActiveTenant(); changed() }
  async function authRequest(url: string, init: RequestInit): Promise<Response> {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        const error = new DOMException('Authentication request timed out', 'TimeoutError')
        controller.abort(error)
        reject(error)
      }, 20_000)
    })
    try {
      // Buffer the small auth response so the deadline includes its JSON body.
      const response = raw(url, { ...init, signal: controller.signal }).then(async response => {
        const body = await response.arrayBuffer()
        return new Response(response.status === 204 ? null : body, {status:response.status, statusText:response.statusText, headers:response.headers})
      })
      return await Promise.race([response, timeout])
    } finally { clearTimeout(timer) }
  }
  async function check(response: Response): Promise<{ access_token: string }> {
    if (!response.ok) throw new ApiError({ status: response.status, code: 'authentication_failed', title: '登录状态恢复失败' })
    return response.json() as Promise<{ access_token: string }>
  }
  async function derive(tenant: ActiveTenant): Promise<string> {
    const response = await authRequest(`${base}/auth/tenant-access`, {
      method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.readToken()}` }, body: JSON.stringify({ tenant_id: tenant.tenant_id, membership_id: tenant.membership_id }),
    })
    return (await check(response)).access_token
  }
  async function ensureSession(force = false): Promise<void> {
    if (refreshing) return refreshing
    if (!state.readToken() && !csrf()) return
    if (!force && !expired(state.readToken()) && (!state.readActiveTenant() || !expired(state.readTenantToken()))) return
    const before = state.readToken()
    const identity = state.accountIdentity()
    refreshing = lock(async () => {
      // Another tab may already have rotated and persisted its new account token.
      if (state.readToken() === before || expired(state.readToken())) {
        const response = await authRequest(`${base}/auth/refresh`, { method: 'POST', credentials: 'include', headers: { 'X-CSRF-Token': csrf() ?? '' } })
        if (state.accountIdentity() !== identity) throw new DOMException('Session changed', 'AbortError')
        if (response.status === 401) { clear(); await check(response); return }
        const result = await check(response)
        if (state.accountIdentity() !== identity) throw new DOMException('Session changed', 'AbortError')
        state.saveToken(result.access_token)
        changed()
      }
      const tenant = state.readActiveTenant()
      if (tenant) {
        const account = state.accountIdentity()
        const token = await derive(tenant)
        if (state.accountIdentity() !== account || state.readActiveTenant()?.membership_id !== tenant.membership_id) throw new DOMException('Session changed', 'AbortError')
        state.saveTenantToken(token)
      }
      changed()
    }).finally(() => { refreshing = null })
    return refreshing
  }
  async function authenticatedFetch(scope: Scope, url: string, init: RequestInit = {}): Promise<Response> {
    if (/\/auth\/(login|register|refresh|logout)(?:\?|$)/.test(url)) return raw(url, { ...init, credentials: 'include' })
    if (expired(state.readToken()) || (scope === 'tenant' && expired(state.readTenantToken()))) await ensureSession()
    const identity = scope === 'tenant' ? state.tenantIdentity() : state.accountIdentity()
    const originalToken = scope === 'tenant' ? state.readTenantToken() : state.readToken()
    const assertIdentity = () => {
      if ((scope === 'tenant' ? state.tenantIdentity() : state.accountIdentity()) !== identity) throw new DOMException('Session changed', 'AbortError')
    }
    const send = () => {
      const headers = new Headers(init.headers)
      const token = scope === 'tenant' ? state.readTenantToken() : state.readToken()
      if (token) headers.set('Authorization', `Bearer ${token}`)
      else headers.delete('Authorization')
      return raw(url, { ...init, headers, credentials: 'include' })
    }
    let response = await send()
    assertIdentity()
    if (response.status === 401) {
      if (!state.readToken() && !csrf()) return response
      if ((scope === 'tenant' ? state.readTenantToken() : state.readToken()) === originalToken) await ensureSession(true)
      assertIdentity()
      init.signal?.throwIfAborted()
      response = await send()
      assertIdentity()
    }
    return response
  }
  async function selectTenant(tenant: ActiveTenant): Promise<void> {
    const generation = ++selectionGeneration
    await ensureSession()
    if (generation !== selectionGeneration) throw new DOMException('Session changed', 'AbortError')
    const identity = state.accountIdentity()
    const token = await derive(tenant)
    if (state.accountIdentity() !== identity || generation !== selectionGeneration) throw new DOMException('Session changed', 'AbortError')
    state.saveActiveTenant(tenant); state.saveTenantToken(token); changed()
  }
  async function logoutSession(): Promise<void> {
    selectionGeneration += 1
    await lock(async () => {
      const response = await authRequest(`${base}/auth/logout`, { method: 'POST', credentials: 'include', headers: { 'X-CSRF-Token': csrf() ?? '' } })
      if (!response.ok && response.status !== 401) await check(response)
      clear()
    })
  }
  function acceptLogin(token: string) { selectionGeneration += 1; state.clearTenantToken(); state.clearActiveTenant(); state.saveToken(token); changed() }
  return { authenticatedFetch, ensureSession, selectTenant, logoutSession, acceptLogin }
}
const transport = createAuthTransport({ session, baseUrl: (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1', onChange: refreshAuth })
export const { authenticatedFetch, ensureSession, selectTenant, logoutSession, acceptLogin } = transport
if (typeof window !== 'undefined') window.addEventListener('storage', refreshAuth)
