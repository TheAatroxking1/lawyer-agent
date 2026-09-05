import { describe, expect, it, vi } from 'vitest'

import { ApiError, createApiClient, queryString } from './client'
import type { FetchLike } from './client'

function jsonResponse(body: unknown, status = 200, contentType = 'application/json') {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': contentType },
  })
}

function problem(status: number, code: string, title: string) {
  return {
    type: 'about:blank',
    title,
    status,
    code,
    trace_id: 'trace-1',
  }
}

describe('queryString', () => {
  it('skips null/undefined/empty values and encodes pairs', () => {
    expect(
      queryString({ limit: 20, title: '契 税', before_id: null, empty: '', kept: 'a/b' }),
    ).toBe('?limit=20&title=%E5%A5%91%20%E7%A8%8E&kept=a%2Fb')
  })

  it('returns empty string for no params', () => {
    expect(queryString(undefined)).toBe('')
  })
})

describe('createApiClient.request', () => {
  it('injects the bearer token from the provider', async () => {
    const fetchImpl: FetchLike = vi.fn(async (url, init) => {
      expect(url).toBe('http://test/api/v1/legal/instruments?limit=5')
      expect(init.headers).toMatchObject({ Authorization: 'Bearer tok-1' })
      return jsonResponse({ items: [], next_before_id: null })
    })
    const client = createApiClient({
      baseUrl: 'http://test/api/v1',
      tokenProvider: () => 'tok-1',
      fetchImpl,
    })
    const page = await client.request('/legal/instruments?limit=5')
    expect(page).toEqual({ items: [], next_before_id: null })
  })

  it('serialises JSON bodies with the content type header', async () => {
    const fetchImpl: FetchLike = vi.fn(async (_url, init) => {
      expect(init.method).toBe('POST')
      expect(new Headers(init.headers).get('content-type')).toBe('application/json')
      expect(init.body).toBe('{"username":"alice"}')
      return jsonResponse({ access_token: 'at', token_type: 'Bearer' })
    })
    const client = createApiClient({
      baseUrl: 'http://test/api/v1',
      tokenProvider: () => null,
      fetchImpl,
    })
    const result = await client.request<{ access_token: string }>('/auth/login', {
      method: 'POST',
      body: { username: 'alice' },
    })
    expect(result.access_token).toBe('at')
  })

  it('maps a Problem Details response to a stable ApiError', async () => {
    const fetchImpl: FetchLike = vi.fn(async () =>
      jsonResponse(problem(422, 'legal_chat_invalid_request', 'Request is invalid'), 422),
    )
    const client = createApiClient({
      baseUrl: 'http://test/api/v1',
      fetchImpl,
    })
    await expect(client.request('/legal/chat', { method: 'POST', body: {} })).rejects.toMatchObject({
      name: 'ApiError',
      status: 422,
      code: 'legal_chat_invalid_request',
      title: 'Request is invalid',
      traceId: 'trace-1',
    })
  })

  it('derives a stable error when the body is not Problem Details', async () => {
    const fetchImpl: FetchLike = vi.fn(async () => jsonResponse({ unexpected: true }, 500))
    const client = createApiClient({ baseUrl: 'http://test/api/v1', fetchImpl })
    await expect(client.request('/x')).rejects.toMatchObject({
      name: 'ApiError',
      status: 500,
      code: 'http_500',
    })
  })

  it('derives authentication_failed for a 401 without a body', async () => {
    const fetchImpl: FetchLike = vi.fn(async () => jsonResponse({}, 401))
    const client = createApiClient({ baseUrl: 'http://test/api/v1', fetchImpl })
    await expect(client.request('/auth/refresh')).rejects.toMatchObject({
      code: 'authentication_failed',
    })
  })

  it('does not attach the bearer token when the provider returns null', async () => {
    const fetchImpl: FetchLike = vi.fn(async (_url, init) => {
      expect(init.headers).not.toHaveProperty('Authorization')
      return jsonResponse([])
    })
    const client = createApiClient({
      baseUrl: 'http://test/api/v1',
      tokenProvider: () => null,
      fetchImpl,
    })
    await client.request('/health')
  })

  it('rejects with AbortError when the request exceeds the timeout', async () => {
    const fetchImpl: FetchLike = vi.fn(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('timed out', 'AbortError')),
          )
        }),
    )
    const client = createApiClient({
      baseUrl: 'http://test/api/v1',
      fetchImpl,
      defaultTimeoutMs: 5_000,
    })
    await expect(client.request('/slow', { timeoutMs: 20 })).rejects.toMatchObject({
      name: 'AbortError',
    })
  })

  it('throws ApiError rather than a JSON parse error for malformed bodies', async () => {
    const fetchImpl: FetchLike = vi.fn(
      async () => new Response('<html>oops</html>', { status: 502 }),
    )
    const client = createApiClient({ baseUrl: 'http://test/api/v1', fetchImpl })
    await expect(client.request('/x')).rejects.toBeInstanceOf(ApiError)
  })
})
