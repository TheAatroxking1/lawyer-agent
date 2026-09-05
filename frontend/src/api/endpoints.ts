// Typed endpoint functions bound to an ApiClient. Each mirrors one backend
// route from backend/src/lawyer_agent/api/v1/*.

import type { ApiClient } from './client'
import { queryString } from './client'
import type {
  AccessTokenResponse,
  AccountTenantList,
  AgentToolCallInput,
  AgentToolCallResult,
  AgentToolInfo,
  ChatMessageInput,
  ChatReply,
  InstrumentListQuery,
  InstrumentPage,
  InstrumentSummary,
  LegalVersionSummary,
  LoginInput,
  ProvisionSummary,
  RegisterInput,
  RetrievalQuestionInput,
  RetrievalQuestionReply,
  SwitchTenantInput,
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

export async function askQuestion(
  client: ApiClient,
  input: RetrievalQuestionInput,
): Promise<RetrievalQuestionReply> {
  return client.request<RetrievalQuestionReply>('/legal/questions', {
    method: 'POST',
    body: input,
  })
}

export async function myTenants(
  client: ApiClient,
): Promise<AccountTenantList> {
  return client.request<AccountTenantList>('/accounts/me/tenants')
}

export async function switchTenant(
  client: ApiClient,
  input: SwitchTenantInput,
): Promise<AccessTokenResponse> {
  return client.request<AccessTokenResponse>('/auth/switch-tenant', {
    method: 'POST',
    body: input,
  })
}

export async function listAgentTools(
  client: ApiClient,
): Promise<AgentToolInfo[]> {
  return client.request<AgentToolInfo[]>('/platform/agent/tools')
}

export async function callAgentTool(
  client: ApiClient,
  input: AgentToolCallInput,
): Promise<AgentToolCallResult> {
  return client.request<AgentToolCallResult>('/platform/agent/tools/call', {
    method: 'POST',
    body: { tool: input.tool, args: input.args ?? {} },
  })
}
