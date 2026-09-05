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
  region_code: string
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
