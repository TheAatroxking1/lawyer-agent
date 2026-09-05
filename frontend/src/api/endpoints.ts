// Typed endpoint functions bound to an ApiClient. Each mirrors one backend
// route from backend/src/lawyer_agent/api/v1/*.

import type { ApiClient } from './client'
import { queryString } from './client'
import type {
  AccessTokenResponse,
  ChatMessageInput,
  ChatReply,
  InstrumentListQuery,
  InstrumentPage,
  InstrumentSummary,
  LegalVersionSummary,
  LoginInput,
  ProvisionSummary,
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

export async function getInstrument(
  client: ApiClient,
  instrumentId: string,
): Promise<InstrumentSummary> {
  return client.request<InstrumentSummary>(
    `/legal/instruments/${encodeURIComponent(instrumentId)}`,
  )
}

export async function listVersionsForInstrument(
  client: ApiClient,
  instrumentId: string,
): Promise<LegalVersionSummary[]> {
  return client.request<LegalVersionSummary[]>(
    `/legal/instruments/${encodeURIComponent(instrumentId)}/versions`,
  )
}

export async function getVersion(
  client: ApiClient,
  versionId: string,
): Promise<LegalVersionSummary> {
  return client.request<LegalVersionSummary>(
    `/legal/versions/${encodeURIComponent(versionId)}`,
  )
}

export async function listProvisionsForVersion(
  client: ApiClient,
  versionId: string,
): Promise<ProvisionSummary[]> {
  return client.request<ProvisionSummary[]>(
    `/legal/versions/${encodeURIComponent(versionId)}/provisions`,
  )
}

export async function chat(
  client: ApiClient,
  messages: ChatMessageInput[],
): Promise<ChatReply> {
  return client.request<ChatReply>('/legal/chat', {
    method: 'POST',
    body: { messages },
  })
}
