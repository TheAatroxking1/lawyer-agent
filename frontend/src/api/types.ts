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
