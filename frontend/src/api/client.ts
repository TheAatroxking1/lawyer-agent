// Thin typed fetch wrapper around the versioned backend API.
//
// - resolves absolute URLs from a base (defaults to the same-origin
//   `/api/v1`, proxied by the Vite dev server to the FastAPI service);
// - injects `Authorization: Bearer <token>` when a token provider is given;
// - maps non-2xx Problem Details responses to a stable ApiError carrying
//   status/code/title so views can render backend error codes verbatim.

import type { ApiProblemBody } from './types'

export const DEFAULT_TIMEOUT_MS = 20_000

const STATUS_TITLES: Record<number, string> = {
  400: 'Bad request',
  401: 'Authentication failed',
  403: 'Permission denied',
  404: 'Not Found',
  409: 'Request conflicts with current state',
  422: 'Request is invalid',
  429: 'Too many requests',
  500: 'Internal server error',
  502: 'Bad gateway',
  503: 'Service unavailable',
  504: 'Gateway timeout',
}

export interface ApiErrorShape {
  status: number
  code: string
  title: string
  traceId?: string
  detail?: string
  headers?: Headers
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly title: string
  readonly traceId?: string
  readonly detail?: string
  readonly headers?: Headers

  constructor(shape: ApiErrorShape) {
    super(`${shape.status} ${shape.code}: ${shape.title}`)
    this.name = 'ApiError'
    this.status = shape.status
    this.code = shape.code
    this.title = shape.title
    this.traceId = shape.traceId
    this.detail = shape.detail
    this.headers = shape.headers
  }
}

export type TokenProvider = () => string | null
export type FetchLike = (
  input: string,
  init: RequestInit,
) => Promise<Response>

export interface ApiClientOptions {
  baseUrl: string
  tokenProvider?: TokenProvider
  fetchImpl?: FetchLike
  defaultTimeoutMs?: number
}

export interface RequestInitLike {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  timeoutMs?: number
}

export interface ApiClient {
  readonly baseUrl: string
  request<T>(path: string, init?: RequestInitLike): Promise<T>
}

function queryString(params: Record<string, unknown> | undefined): string {
  if (!params) return ''
  const pairs: string[] = []
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    pairs.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
  }
  return pairs.length > 0 ? `?${pairs.join('&')}` : ''
}

function deriveError(
  status: number,
  body: ApiProblemBody | null,
  headers: Headers,
): ApiError {
  if (body && typeof body.code === 'string' && typeof body.title === 'string') {
    return new ApiError({
      status: body.status ?? status,
      code: body.code,
      title: body.title,
      traceId: body.trace_id,
      headers,
    })
  }
  const title = STATUS_TITLES[status] ?? 'Unexpected server error'
  const code = status === 401 ? 'authentication_failed' : `http_${status}`
  return new ApiError({ status, code, title, headers })
}

export function createApiClient(options: ApiClientOptions): ApiClient {
  const {
    baseUrl,
    tokenProvider = () => null,
    fetchImpl = fetch,
    defaultTimeoutMs = DEFAULT_TIMEOUT_MS,
  } = options

  async function request<T>(path: string, init?: RequestInitLike): Promise<T> {
    const url = path.startsWith('http') ? path : `${baseUrl}${path}`
    const controller = new AbortController()
    const timeoutMs = init?.timeoutMs ?? defaultTimeoutMs
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    try {
      const token = tokenProvider()
      const headers: Record<string, string> = {
        Accept: 'application/json, application/problem+json',
      }
      if (token) headers.Authorization = `Bearer ${token}`
      let body: string | undefined
      if (init?.body !== undefined) {
        headers['Content-Type'] = 'application/json'
        body = JSON.stringify(init.body)
      }
      const response = await fetchImpl(url, {
        method: init?.method ?? 'GET',
        headers,
        body,
        signal: controller.signal,
      })
      if (response.status === 204) {
        return undefined as T
      }
      const contentType = response.headers.get('content-type') ?? ''
      let parsed: ApiProblemBody | null = null
      if (contentType.includes('json')) {
        const text = await response.text()
        if (text.length > 0) {
          try {
            parsed = JSON.parse(text) as ApiProblemBody
          } catch {
            parsed = null
          }
        }
      }
      if (!response.ok) {
        throw deriveError(response.status, parsed, response.headers)
      }
      return parsed as T
    } finally {
      clearTimeout(timer)
    }
  }

  return { baseUrl, request }
}

export { queryString }
