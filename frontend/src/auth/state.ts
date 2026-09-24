// Reactive mirror of the access-token session so components re-render when
// the token changes (login modal, shell header, chat send gating).

import { reactive } from 'vue'

import { session, type ActiveTenant } from './session'

export interface AuthState {
  token: string | null
  authenticated: boolean
  activeTenant: ActiveTenant | null
  accountIdentity: string | null
  tenantIdentity: string | null
}

function snapshot(): AuthState {
  const token = session.readToken()
  return { token, authenticated: typeof token === 'string' && token.length > 0, activeTenant: session.readActiveTenant(), accountIdentity: session.accountIdentity(), tenantIdentity: session.tenantIdentity() }
}

export const authState: AuthState = reactive(snapshot())

export function refreshAuth(): void {
  const next = snapshot()
  authState.token = next.token
  authState.authenticated = next.authenticated
  authState.activeTenant = next.activeTenant
  authState.accountIdentity = next.accountIdentity
  authState.tenantIdentity = next.tenantIdentity
}
