// Application-wide ApiClient: same-origin `/api/v1` in dev is proxied to the
// FastAPI service by Vite; override with VITE_API_BASE for other setups.

import { session } from '../auth/session'
import { createApiClient } from './client'

const apiBase: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1'

export const apiClient = createApiClient({
  baseUrl: apiBase,
  tokenProvider: () => session.readToken(),
})

export * from './client'
export * from './endpoints'
export * from './types'
