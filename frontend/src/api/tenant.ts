// Tenant-scoped API access for the case-document workbench.
//
// These calls run with the switched tenant access token so the backend binds
// them to the active membership; the account token stays untouched.

import { createApiClient } from './client'
import { session } from '../auth/session'
import { authenticatedFetch } from '../auth/transport'
import type {
  CreateMatterInput,
  DocumentHeaderSummary,
  DocumentSummary,
  MatterPage,
  MatterSummary,
  RegisterDocumentInput,
  RiskIssueSummary,
} from './types'

const apiBase: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1'

export const tenantApiClient = createApiClient({
  baseUrl: apiBase,
  tokenProvider: () => session.readTenantToken(),
  fetchImpl: (url, init) => authenticatedFetch('tenant', url, init),
})

export async function listMatters(
  tenantId: string,
): Promise<MatterPage> {
  return tenantApiClient.request<MatterPage>(
    `/tenants/${encodeURIComponent(tenantId)}/matters?limit=50`,
  )
}

export async function createMatter(
  tenantId: string,
  input: CreateMatterInput,
): Promise<MatterSummary> {
  return tenantApiClient.request<MatterSummary>(
    `/tenants/${encodeURIComponent(tenantId)}/matters`,
    { method: 'POST', body: input },
  )
}

export async function listDocuments(
  tenantId: string,
  matterId: string,
): Promise<DocumentHeaderSummary[]> {
  return tenantApiClient.request<DocumentHeaderSummary[]>(
    `/tenants/${encodeURIComponent(tenantId)}/matters/${encodeURIComponent(matterId)}/documents`,
  )
}

export async function registerDocument(
  tenantId: string,
  matterId: string,
  input: RegisterDocumentInput,
): Promise<DocumentSummary> {
  return tenantApiClient.request<DocumentSummary>(
    `/tenants/${encodeURIComponent(tenantId)}/matters/${encodeURIComponent(matterId)}/documents`,
    { method: 'POST', body: input },
  )
}

export async function listRiskIssues(
  tenantId: string,
  documentId: string,
): Promise<RiskIssueSummary[]> {
  return tenantApiClient.request<RiskIssueSummary[]>(
    `/tenants/${encodeURIComponent(tenantId)}/documents/${encodeURIComponent(documentId)}/risk-issues`,
  )
}

export async function downloadReport(
  tenantId: string,
  documentId: string,
  signal?: AbortSignal,
): Promise<{ fileName: string; blob: Blob }> {
  const headers: Record<string, string> = {}
  const token = session.readTenantToken()
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await authenticatedFetch('tenant',
    `${apiBase}/tenants/${encodeURIComponent(tenantId)}/documents/${encodeURIComponent(documentId)}/report.docx`,
    { headers, signal },
  )
  if (!response.ok) {
    throw new Error(`report download failed with HTTP ${response.status}`)
  }
  return { fileName: 'rule-check-report.docx', blob: await response.blob() }
}
