import { describe, expect, it } from 'vitest'

import { createSession } from './session'
import type { TokenStorage } from './session'

function memoryStorage(): TokenStorage {
  const values = new Map<string, string>()
  return {
    getItem(key: string): string | null {
      return values.get(key) ?? null
    },
    setItem(key: string, value: string): void {
      values.set(key, value)
    },
    removeItem(key: string): void {
      values.delete(key)
    },
  }
}

describe('createSession', () => {
  it('persists, reads and clears the access token', () => {
    const storage = memoryStorage()
    const session = createSession(storage)
    expect(session.isAuthenticated()).toBe(false)
    session.saveToken('abc.def')
    expect(session.readToken()).toBe('abc.def')
    expect(session.isAuthenticated()).toBe(true)
    expect(storage.getItem('lawyer_agent.access_token')).toBe('abc.def')
    session.clearToken()
    expect(session.readToken()).toBeNull()
    expect(session.isAuthenticated()).toBe(false)
  })

  it('persists a tenant access token separately from the account token', () => {
    const storage = memoryStorage()
    const session = createSession(storage)
    session.saveToken('account-token')
    expect(session.readTenantToken()).toBeNull()
    session.saveTenantToken('tenant-token')
    expect(session.readTenantToken()).toBe('tenant-token')
    expect(session.readToken()).toBe('account-token')
    session.clearTenantToken()
    expect(session.readTenantToken()).toBeNull()
    expect(session.readToken()).toBe('account-token')
  })

  it('treats an empty stored token as not authenticated', () => {
    const storage = memoryStorage()
    storage.setItem('lawyer_agent.access_token', '')
    const session = createSession(storage)
    expect(session.isAuthenticated()).toBe(false)
  })

  it('degrades to an in-memory no-op session without storage', () => {
    const session = createSession(null)
    session.saveToken('token')
    expect(session.readToken()).toBeNull()
    expect(session.isAuthenticated()).toBe(false)
  })
})
