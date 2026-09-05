// Typed endpoint functions bound to an ApiClient. Each mirrors one backend
// route from backend/src/lawyer_agent/api/v1/*.

import type { ApiClient } from './client'
import { queryString } from './client'
import type {
  AccessTokenResponse,
  InstrumentListQuery,
  InstrumentPage,
  LoginInput,
  RegisterInput,
} from './types'

export async function login(
  client: ApiClient,
  input: LoginInput,
): Promise<AccessTokenResponse> {
  return client.request<AccessTokenResponse>('/auth/login', {
    method: 'POST',
    body: input,
  })
}

export async function registerAccount(
  client: ApiClient,
  input: RegisterInput,
): Promise<AccessTokenResponse> {
  return client.request<AccessTokenResponse>('/auth/register', {
    method: 'POST',
    body: input,
  })
}

export async function listInstruments(
  client: ApiClient,
  query: InstrumentListQuery = {},
): Promise<InstrumentPage> {
  const suffix = queryString({ ...query })
  return client.request<InstrumentPage>(`/legal/instruments${suffix}`)
}
