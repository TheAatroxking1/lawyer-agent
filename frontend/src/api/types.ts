// Strongly typed mirrors of the backend HTTP contracts the web client uses.
// Only fields the backend actually returns are modelled; bodies are never
// forwarded as arbitrary objects.

export interface AccessTokenResponse {
  access_token: string
  token_type: 'Bearer'
}

export type LoginKind =
  | 'username'
  | 'phone'
  | 'email'
  | 'wechat_unionid'
  | 'wechat_openid'

export interface LoginInput {
  kind: LoginKind
  identifier: string
  password: string
}

export interface RegisterInput {
  username: string
  password: string
  display_name: string
}

// GET /api/v1/legal/instruments (LegalInstrumentSummary / LegalInstrumentPage)
export interface InstrumentSummary {
  id: string
  title: string
  issuing_authority: string
  jurisdiction: string
  region_code: string | null
}

export interface InstrumentPage {
  items: InstrumentSummary[]
  next_before_id: string | null
}

export interface InstrumentListQuery {
  limit?: number
  before_id?: string | null
  title?: string
  issuing_authority?: string
  jurisdiction?: string
  region_code?: string
}

// GET /api/v1/legal/instruments/{id}/versions and /legal/versions/{id}
export interface LegalVersionSummary {
  id: string
  instrument_id: string
  version_label: string
  status: string
  published_on: string | null
  effective_on: string | null
  repealed_on: string | null
  law_number: string | null
  source_ref: string | null
  dataset_version: string | null
  parser_version: string | null
}

// GET /api/v1/legal/versions/{id}/provisions
export interface ProvisionSummary {
  id: string
  version_id: string
  provision_no: string
  level: string
  structure_path: string[]
  title: string | null
  full_text: string
}

// POST /api/v1/legal/chat (backend/api/v1/legal_chat.py)
export interface ChatMessageInput {
  role: 'system' | 'user' | 'assistant'
  content: string
}

export interface ChatUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface ChatReply {
  text: string
  usage: ChatUsage
}

// POST /api/v1/legal/questions (+ /questions/stream answer payload)
export interface RetrievalCitation {
  evidence_id: string
  instrument_title: string
  version_label: string
  provision_no: string
  provision_text: string
  source_ref: string
  dataset_version: string
}

export interface RetrievalQuestionReply {
  refused: boolean
  reason: string
  text: string
  usage: ChatUsage | null
  citations: RetrievalCitation[]
}

export interface RetrievalQuestionInput {
  question: string
  alias?: string
  target_date?: string
}

// GET /api/v1/accounts/me/tenants and POST /api/v1/auth/switch-tenant
export interface AccountTenant {
  tenant_id: string
  membership_id: string
  name: string
  tenant_type: string
  tenant_status: string
  membership_status: string
}

export interface AccountTenantList {
  items: AccountTenant[]
}

export interface SwitchTenantInput {
  tenant_id: string
  membership_id: string
}

// Tenant matter/document contracts (backend/api/v1/matter_documents.py)
export type MatterKind = 'contract_review' | 'litigation' | 'legal_advice' | 'compliance' | 'other'

export interface MatterSummary {
  id: string
  title: string
  kind: string
  status: string
  description: string | null
  owner_membership_id: string | null
  version: number
}

export interface MatterPage {
  items: MatterSummary[]
  next_before_id: string | null
}

export interface CreateMatterInput {
  title: string
  kind: MatterKind
  description?: string
}

export interface RegisterDocumentInput {
  file_name: string
  mime_type: string
  payload_b64: string
}

export interface DocumentSummary {
  id: string
  document_id: string
  version_no: number
  kind: string
  file_name: string | null
  upload_status: string
  review_status: string | null
}

export interface DocumentHeaderSummary {
  id: string
  display_name: string
  current_version_no: number
}

// Agent tool gateway (backend/api/v1/agent_gateway.py)
export interface AgentToolInfo {
  name: string
  description: string
}

export interface AgentToolCallResult {
  ok: boolean
  output: unknown | null
  error_code: string | null
  error_message: string | null
}

export interface AgentToolCallInput {
  tool: string
  args?: Record<string, unknown>
}

// Problem Details body produced by backend/api/errors.py
export interface ProblemErrors {
  field?: string
  code?: string
}

export interface ApiProblemBody {
  type?: string
  title?: string
  status?: number
  code?: string
  trace_id?: string
  errors?: ProblemErrors[]
}
