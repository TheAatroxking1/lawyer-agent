import { describe, expect, it, vi } from 'vitest'
import { createSession } from './session'
import { createAuthTransport } from './transport'
const token = (nonce: string, user = 'user') => `e30.${btoa(JSON.stringify({ sub: user, sid: 'session', auth_version: 1, exp: 9999999999, jti: nonce }))}.sig`

function setup() {
  const values = new Map<string, string>()
  const session = createSession({ getItem: k => values.get(k) ?? null, setItem: (k,v) => { values.set(k,v) }, removeItem: k => { values.delete(k) } })
  session.saveToken(token('old'))
  return session
}
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } })
describe('remembered authentication transport', () => {
  it('leaves guests quiet and returns protected 401 without a refresh attempt', async () => {
    const session = setup()
    session.clearToken()
    const fetchImpl = vi.fn(async () => json({},401))
    const transport = createAuthTransport({session, fetchImpl, csrfProvider: () => null})
    await transport.ensureSession()
    expect(fetchImpl).not.toHaveBeenCalled()
    expect((await transport.authenticatedFetch('account','/private')).status).toBe(401)
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it.each(['refresh', 'derive', 'logout'])('bounds %s to 20 seconds while retaining state', async operation => {
    vi.useFakeTimers()
    try {
      const session = setup()
      let signal: AbortSignal | null | undefined
      const fetchImpl = vi.fn((_url: string, init: RequestInit) => { signal = init.signal; return new Promise<Response>(() => {}) })
      const transport = createAuthTransport({session, fetchImpl})
      const pending = operation === 'refresh' ? transport.ensureSession(true) : operation === 'derive' ? transport.selectTenant({tenant_id:'t',membership_id:'m'}) : transport.logoutSession()
      let failure: Error | undefined
      void pending.catch((error: Error) => { failure = error })
      await vi.advanceTimersByTimeAsync(20_000)
      expect(failure?.message).toContain('timed out')
      expect(signal?.aborted).toBe(true)
      expect(session.readToken()).toBe(token('old'))
    } finally { vi.useRealTimers() }
  })
  it.each(['newer selection', 'logout', 'login'])('does not let a late tenant selection overwrite %s', async action => {
    const session = setup()
    let release!: (response: Response) => void
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      if (String(init.body).includes('old-tenant')) return new Promise<Response>(resolve => { release = resolve })
      return json({access_token:'new-tenant-token'})
    })
    const transport = createAuthTransport({session, fetchImpl})
    const old = transport.selectTenant({tenant_id:'old-tenant',membership_id:'old-member'})
    const rejected = expect(old).rejects.toThrow('Session changed')
    await vi.waitFor(() => expect(release).toBeDefined())
    if (action === 'newer selection') await transport.selectTenant({tenant_id:'new-tenant',membership_id:'new-member'})
    if (action === 'logout') await transport.logoutSession()
    if (action === 'login') transport.acceptLogin(token('login'))
    release(json({access_token:'old-tenant-token'}))
    await rejected
    expect(session.readActiveTenant()?.tenant_id ?? null).toBe(action === 'newer selection' ? 'new-tenant' : null)
  })
  it('restores account and selected tenant from persisted cookie state', async () => {
    const session = setup()
    session.clearToken()
    session.saveActiveTenant({ tenant_id: 't', membership_id: 'm' })
    const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
      expect(init.credentials).toBe('include')
      return json({access_token: url.endsWith('/refresh') ? token('restored') : 'tenant-restored'})
    })
    const transport = createAuthTransport({session, fetchImpl, csrfProvider: () => 'cookie'})
    await transport.ensureSession()
    expect(session.readToken()).toBe(token('restored'))
    expect(session.readTenantToken()).toBe('tenant-restored')
    expect(session.tenantIdentity()).toBe('user:session:1:t:m')
  })
  it('does not clear a new login when an old refresh rejection arrives late', async () => {
    const session = setup()
    const fetchImpl = vi.fn(async () => { session.saveToken(token('other', 'other')); return json({},401) })
    const transport = createAuthTransport({session, fetchImpl})
    await expect(transport.ensureSession(true)).rejects.toThrow()
    expect(session.readToken()).toBe(token('other','other'))
  })
  it('returns the opened stream without replay after a stream failure', async () => {
    const session = setup()
    const fetchImpl = vi.fn(async () => new Response(new ReadableStream({start(controller) { controller.error(new Error('stream interrupted')) }})))
    const transport = createAuthTransport({session, fetchImpl})
    const response = await transport.authenticatedFetch('account', '/stream', {method:'POST'})
    await expect(response.text()).rejects.toThrow('stream interrupted')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('merges simultaneous 401 refresh and retries each request once', async () => {
    const session = setup()
    const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
      if (url.endsWith('/auth/refresh')) return json({ access_token: token('new') })
      return new Headers(init.headers).get('Authorization') === `Bearer ${token('new')}` ? json({ ok: true }) : json({}, 401)
    })
    const transport = createAuthTransport({ session, fetchImpl, csrfProvider: () => 'csrf' })
    await Promise.all([transport.authenticatedFetch('account', '/one', {}), transport.authenticatedFetch('account', '/two', {})])
    expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/auth/refresh'))).toHaveLength(1)
    expect(session.readToken()).toBe(token('new'))
  })
  it('retains login on network failure but clears it when refresh is rejected', async () => {
    const session = setup()
    const fetchImpl = vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValueOnce(json({},401))
    const transport = createAuthTransport({ session, fetchImpl })
    await expect(transport.ensureSession(true)).rejects.toThrow('offline')
    expect(session.readToken()).toBe(token('old'))
    await expect(transport.ensureSession(true)).rejects.toThrow()
    expect(session.readToken()).toBeNull()
  })
  it('derives a tenant token without replacing the account and revokes on logout', async () => {
    const session = setup()
    const fetchImpl = vi.fn(async (_url: string, _init: RequestInit) => json({ access_token: 'tenant' }))
    const transport = createAuthTransport({ session, fetchImpl, csrfProvider: () => 'rotated' })
    await transport.selectTenant({ tenant_id: 't', membership_id: 'm' })
    expect(session.readToken()).toBe(token('old'))
    expect(session.readTenantToken()).toBe('tenant')
    await transport.logoutSession()
    expect(fetchImpl.mock.calls.at(-1)?.[0]).toBe('/api/v1/auth/logout')
    expect(session.readToken()).toBeNull()
    expect(session.readActiveTenant()).toBeNull()
  })
  it('does not retry a delayed 401 with another user identity', async () => {
    const session = setup()
    const fetchImpl = vi.fn(async (url: string) => {
      if (url === '/private') { session.saveToken(token('other', 'other')); return json({},401) }
      return json({access_token: token('other-new', 'other')})
    })
    const transport = createAuthTransport({session, fetchImpl})
    await expect(transport.authenticatedFetch('account','/private')).rejects.toThrow('Session changed')
    expect(fetchImpl.mock.calls.filter(([url]) => url === '/private')).toHaveLength(1)
  })
  it('uses the already refreshed token after a late concurrent 401', async () => {
    const session = setup()
    let release!: () => void
    const delayed = new Promise<void>(resolve => { release = resolve })
    const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
      if (url.endsWith('/auth/refresh')) return json({access_token: token('new')})
      if (new Headers(init.headers).get('Authorization') === `Bearer ${token('new')}`) return json({ok:true})
      if (url === '/slow') await delayed
      return json({},401)
    })
    const transport = createAuthTransport({session, fetchImpl})
    const slow = transport.authenticatedFetch('account','/slow')
    await transport.authenticatedFetch('account','/fast')
    release(); await slow
    expect(fetchImpl.mock.calls.filter(([url]) => url.endsWith('/auth/refresh'))).toHaveLength(1)
  })
  it('rechecks persisted tokens inside a cross-tab lock without rotating twice', async () => {
    const session = setup()
    const fetchImpl = vi.fn(async () => json({access_token: token('unexpected')}))
    const transport = createAuthTransport({session, fetchImpl, lock: async action => { session.saveToken(token('other-tab')); return action() }})
    await transport.ensureSession(true)
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(session.readToken()).toBe(token('other-tab'))
  })
  it('reads the new csrf cookie at logout and does not clear on failed revocation', async () => {
    const session = setup()
    let csrf = 'before'
    const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
      if (url.endsWith('/refresh')) { csrf = 'after'; return json({access_token:token('new')}) }
      expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('after')
      return json({},503)
    })
    const transport = createAuthTransport({session, fetchImpl, csrfProvider: () => csrf})
    await transport.ensureSession(true)
    await expect(transport.logoutSession()).rejects.toThrow()
    expect(session.readToken()).toBe(token('new'))
  })
})
