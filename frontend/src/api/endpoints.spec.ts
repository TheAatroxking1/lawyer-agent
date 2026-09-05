import { describe, expect, it, vi } from 'vitest'

import type { ApiClient } from './client'
import {
  chat,
  getInstrument,
  getVersion,
  listInstruments,
  listProvisionsForVersion,
  listVersionsForInstrument,
  login,
  registerAccount,
} from './endpoints'

function recordingClient() {
  const calls: Array<{ url: string; init?: unknown }> = []
  const request = vi.fn(async (url: string, init?: unknown) => {
    calls.push({ url, init })
    return {}
  })
  const client = { request } as unknown as ApiClient
  return { client, calls }
}

describe('endpoint path composition', () => {
  it('posts login and register to /auth/*', async () => {
    const { client, calls } = recordingClient()
    await login(client, { kind: 'username', identifier: 'alice', password: 'pw' })
    await registerAccount(client, { username: 'alice', password: 'pw', display_name: 'A' })
    expect(calls[0].url).toBe('/auth/login')
    expect(calls[1].url).toBe('/auth/register')
  })

  it('lists instruments with a trimmed query string', async () => {
    const { client, calls } = recordingClient()
    await listInstruments(client, { limit: 20, title: '契税法', before_id: null })
    expect(calls[0].url).toBe('/legal/instruments?limit=20&title=%E5%A5%91%E7%A8%8E%E6%B3%95')
  })

  it('builds detail, versions, version and provisions paths', async () => {
    const { client, calls } = recordingClient()
    await getInstrument(client, '11111111-1111-7111-8111-111111111111')
    await listVersionsForInstrument(client, '22222222-2222-7222-8222-222222222222')
    await getVersion(client, '33333333-3333-7333-8333-333333333333')
    await listProvisionsForVersion(client, '44444444-4444-7444-8444-444444444444')
    expect(calls.map((call) => call.url)).toEqual([
      '/legal/instruments/11111111-1111-7111-8111-111111111111',
      '/legal/instruments/22222222-2222-7222-8222-222222222222/versions',
      '/legal/versions/33333333-3333-7333-8333-333333333333',
      '/legal/versions/44444444-4444-7444-8444-444444444444/provisions',
    ])
  })

  it('posts chat with the composed message body', async () => {
    const { client, calls } = recordingClient()
    await chat(client, [{ role: 'user', content: '你好' }])
    expect(calls[0].url).toBe('/legal/chat')
    expect(calls[0].init).toEqual({ method: 'POST', body: { messages: [{ role: 'user', content: '你好' }] } })
  })
})
