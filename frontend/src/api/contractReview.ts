import { ApiError } from './client'
import { authenticatedFetch } from '../auth/transport'
import { parseSseBlock } from '../lib/sse'

export type ReviewStage =
  | 'parsing'
  | 'content_warning'
  | 'preparing'
  | 'researching'
  | 'contract_understanding'
  | 'legal_search'
  | 'legal_selection'
  | 'legal_read'
  | 'reviewing'
  | 'validating'
  | 'saved'
  | 'no_evidence'

export interface ReviewAnchor {
  anchor_id: string
  page: number
  start: number
  end: number
  quad: [number, number, number, number, number, number, number, number]
}

export interface ReviewBlock {
  block_id: string
  text: string
  anchors: ReviewAnchor[]
  quality?: string
}

export interface ReviewHighlight {
  block_id: string
  anchor_id: string
  start: number
  end: number
}

export interface ReviewIssue {
  block_id: string
  anchor_id: string | null
  start: number
  end: number
  quote: string
  category: 'wording' | 'commercial' | 'legal'
  severity: 'low' | 'medium' | 'high'
  problem: string
  suggestion: string
  evidence_ids: string[]
  evidence_passages?: Array<{ passage_id: string; document_id: string; quote: string }>
  highlights?: ReviewHighlight[]
  highlights_complete?: boolean
}

export interface ContractReviewRun {
  id: string
  status: string
  file_name: string
  document_version_id?: string
  page_dimensions: Array<[number, number]>
  issues: ReviewIssue[]
  evidence_documents?: Array<{ document_id: string; title: string }>
  mcp_tools?: Array<{ name: string; capability: string; description: string; status: 'discovered' | 'authorized' }>
  is_demo?: boolean
  withheld_issues?: Array<{ index: number; page: number; reason: 'unverified_citation' }>
  blocks: ReviewBlock[]
  failure_code?: string
  verification_report?: { pages: Array<{ page: number; reasons: string[] }> }
  message?: string
}

export type ContractReviewEvent =
  | { type: 'progress'; stage: ReviewStage }
  | { type: 'result'; run: ContractReviewRun }
  | { type: 'error'; code: string; title: string }
  | { type: 'done'; status: string }
  | { type: 'keepalive' }

export interface StartReviewInput {
  tenantId: string
  file: File
  instruction?: string
  asOf?: string
}

interface ContractReviewApiOptions {
  baseUrl: string
  tokenProvider?: () => string | null
  fetchImpl?: (input: string, init: RequestInit) => Promise<Response>
  idFactory?: () => string
}

export interface ContractReviewApi {
  create(tenantId: string, input: { file_name: string; instruction?: string; as_of?: string }, signal?: AbortSignal): Promise<ContractReviewRun>
  upload(tenantId: string, runId: string, bytes: ArrayBuffer, signal?: AbortSignal): Promise<void>
  events(tenantId: string, runId: string, signal?: AbortSignal): Promise<AsyncIterable<ContractReviewEvent>>
  demoEvents(tenantId: string, signal?: AbortSignal): Promise<AsyncIterable<ContractReviewEvent>>
  get(tenantId: string, runId: string, signal?: AbortSignal): Promise<ContractReviewRun>
  document(tenantId: string, runId: string, signal?: AbortSignal): Promise<Uint8Array>
  demoDocument(tenantId: string, signal?: AbortSignal): Promise<Uint8Array>
}

export interface ReviewRequestIdentity {
  generation: number
  tenantId: string
  token: string
}

export function createReviewRequestGuard(tokenProvider: () => string | null) {
  let generation = 0
  return {
    begin(tenantId: string): ReviewRequestIdentity {
      generation += 1
      return { generation, tenantId, token: tokenProvider() ?? '' }
    },
    invalidate(): void {
      generation += 1
    },
    isCurrent(identity: ReviewRequestIdentity, tenantId: string | null): boolean {
      return identity.generation === generation
        && identity.tenantId === tenantId
        && identity.token === (tokenProvider() ?? '')
    },
  }
}

export function createAsyncGenerationGuard() {
  let generation = 0
  return {
    begin(): number {
      generation += 1
      return generation
    },
    invalidate(): void {
      generation += 1
    },
    isCurrent(identity: number): boolean {
      return identity === generation
    },
  }
}

const STAGES = new Set<ReviewStage>([
  'parsing',
  'content_warning',
  'preparing',
  'researching',
  'contract_understanding',
  'legal_search',
  'legal_selection',
  'legal_read',
  'reviewing',
  'validating',
  'saved',
  'no_evidence',
])

export const MAX_PDF_BYTES = 10 * 1024 * 1024
export const MAX_CANVAS_SIDE = 4096
export const MAX_CANVAS_AREA = 12_000_000

function apiError(status: number, code: string, title: string): ApiError {
  return new ApiError({ status, code, title })
}

export async function readPdfPreview(
  file: File,
  maximumBytes = MAX_PDF_BYTES,
): Promise<Uint8Array> {
  if (file.size > maximumBytes) throw apiError(422, 'file_too_large', 'PDF 超出当前大小限制')
  return new Uint8Array(await file.arrayBuffer())
}

export function boundedCanvasScale(width: number, height: number): number {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new Error('invalid_pdf_page')
  }
  return Math.min(
    1.35,
    780 / width,
    MAX_CANVAS_SIDE / width,
    MAX_CANVAS_SIDE / height,
    Math.sqrt(MAX_CANVAS_AREA / (width * height)),
  )
}

export function overlayPercent(
  left: number,
  top: number,
  width: number,
  height: number,
  pageWidth: number,
  pageHeight: number,
): Record<'left' | 'top' | 'width' | 'height', string> {
  if (![left, top, width, height, pageWidth, pageHeight].every(Number.isFinite)
    || pageWidth <= 0 || pageHeight <= 0 || width <= 0 || height <= 0) {
    throw new Error('invalid_overlay_geometry')
  }
  return {
    left: `${left / pageWidth * 100}%`,
    top: `${top / pageHeight * 100}%`,
    width: `${width / pageWidth * 100}%`,
    height: `${height / pageHeight * 100}%`,
  }
}

function assertRun(value: unknown): ContractReviewRun {
  if (!value || typeof value !== 'object') throw apiError(0, 'invalid_response', '服务返回了无效结果')
  const run = value as Partial<ContractReviewRun>
  if (
    typeof run.id !== 'string' ||
    typeof run.status !== 'string' ||
    typeof run.file_name !== 'string' ||
    !Array.isArray(run.page_dimensions) ||
    !Array.isArray(run.issues) ||
    !Array.isArray(run.blocks)
  ) {
    throw apiError(0, 'invalid_response', '服务返回了无效结果')
  }
  return run as ContractReviewRun
}

async function problem(response: Response): Promise<ApiError> {
  try {
    const body = await response.json() as { code?: unknown; title?: unknown }
    if (typeof body.code === 'string' && typeof body.title === 'string') {
      return apiError(response.status, body.code, body.title)
    }
  } catch {
    // Fall through to a stable transport error without reflecting response text.
  }
  return apiError(response.status, `http_${response.status}`, '合同审查请求失败')
}

function parseEvent(event: string, data: string): ContractReviewEvent {
  if (event === 'keepalive') return { type: 'keepalive' }
  let value: Record<string, unknown>
  try {
    value = JSON.parse(data) as Record<string, unknown>
  } catch {
    throw apiError(0, 'invalid_event', '合同审查进度格式无效')
  }
  if (event === 'progress' && typeof value.stage === 'string' && STAGES.has(value.stage as ReviewStage)) {
    return { type: 'progress', stage: value.stage as ReviewStage }
  }
  if (event === 'result') return { type: 'result', run: assertRun(value) }
  if (event === 'error' && typeof value.code === 'string' && typeof value.title === 'string') {
    return { type: 'error', code: value.code, title: value.title }
  }
  if (event === 'done' && typeof value.status === 'string') {
    return { type: 'done', status: value.status }
  }
  throw apiError(0, 'invalid_event', '合同审查进度包含未知事件')
}

export function createContractReviewApi(options: ContractReviewApiOptions): ContractReviewApi {
  const fetchImpl = options.fetchImpl ?? ((url: string, init: RequestInit) => authenticatedFetch('tenant', url, init))
  const tokenProvider = options.tokenProvider ?? (() => null)
  const idFactory = options.idFactory ?? (() => crypto.randomUUID())

  function path(tenantId: string, suffix = ''): string {
    return `${options.baseUrl}/tenants/${encodeURIComponent(tenantId)}/contract-reviews${suffix}`
  }

  function headers(extra: Record<string, string> = {}): Record<string, string> {
    const token = tokenProvider()
    return token ? { ...extra, Authorization: `Bearer ${token}` } : extra
  }

  async function jsonRequest(url: string, init: RequestInit): Promise<unknown> {
    const response = await fetchImpl(url, init)
    if (!response.ok) throw await problem(response)
    return response.status === 204 ? undefined : response.json()
  }

  async function streamEvents(url: string, signal?: AbortSignal): Promise<AsyncIterable<ContractReviewEvent>> {
    const response = await fetchImpl(url, {
      method: 'POST',
      headers: headers({ Accept: 'text/event-stream' }),
      signal,
    })
    if (!response.ok) throw await problem(response)
    if (!response.body) throw apiError(0, 'stream_unavailable', '合同审查进度流不可用')
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    async function* stream(): AsyncIterable<ContractReviewEvent> {
      let buffer = ''
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true }).replaceAll('\r\n', '\n')
          let boundary = buffer.indexOf('\n\n')
          while (boundary >= 0) {
            const frame = parseSseBlock(buffer.slice(0, boundary))
            buffer = buffer.slice(boundary + 2)
            if (frame.event) yield parseEvent(frame.event, frame.data ?? '{}')
            boundary = buffer.indexOf('\n\n')
          }
        }
      } finally {
        reader.releaseLock()
      }
    }
    return stream()
  }

  return {
    async create(tenantId, input, signal) {
      const body: Record<string, string> = { file_name: input.file_name }
      if (input.instruction?.trim()) body.instruction = input.instruction.trim()
      if (input.as_of) body.as_of = input.as_of
      return assertRun(await jsonRequest(path(tenantId), {
        method: 'POST',
        headers: headers({
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'Idempotency-Key': idFactory(),
        }),
        body: JSON.stringify(body),
        signal,
      }))
    },
    async upload(tenantId, runId, bytes, signal) {
      await jsonRequest(path(tenantId, `/${encodeURIComponent(runId)}/document`), {
        method: 'PUT',
        headers: headers({ Accept: 'application/json', 'Content-Type': 'application/pdf' }),
        body: bytes,
        signal,
      })
    },
    async events(tenantId, runId, signal) {
      return streamEvents(path(tenantId, `/${encodeURIComponent(runId)}/events`), signal)
    },
    async demoEvents(tenantId, signal) {
      return streamEvents(path(tenantId, '/demo/events'), signal)
    },
    async get(tenantId, runId, signal) {
      return assertRun(await jsonRequest(path(tenantId, `/${encodeURIComponent(runId)}`), {
        method: 'GET', headers: headers({ Accept: 'application/json' }), signal,
      }))
    },
    async document(tenantId, runId, signal) {
      const response = await fetchImpl(path(tenantId, `/${encodeURIComponent(runId)}/document`), {
        method: 'GET', headers: headers({ Accept: 'application/pdf' }), signal,
      })
      if (!response.ok) throw await problem(response)
      return new Uint8Array(await response.arrayBuffer())
    },
    async demoDocument(tenantId, signal) {
      const response = await fetchImpl(path(tenantId, '/demo/document'), {
        method: 'GET', headers: headers({ Accept: 'application/pdf' }), signal,
      })
      if (!response.ok) throw await problem(response)
      return new Uint8Array(await response.arrayBuffer())
    },
  }
}

export function isCompletedReview(run: ContractReviewRun): boolean {
  return run.status === 'draft' || run.status === 'no_evidence'
}

export async function runContractReview(
  api: ContractReviewApi,
  input: StartReviewInput,
  onEvent: (event: ContractReviewEvent) => void,
  signal?: AbortSignal,
): Promise<{ run: ContractReviewRun; pdf: Uint8Array }> {
  if (input.file.type !== 'application/pdf' && !input.file.name.toLowerCase().endsWith('.pdf')) {
    throw apiError(422, 'pdf_required', '第一版仅支持 PDF 合同')
  }
  const created = await api.create(input.tenantId, {
    file_name: input.file.name,
    instruction: input.instruction,
    as_of: input.asOf,
  }, signal)
  await api.upload(input.tenantId, created.id, await input.file.arrayBuffer(), signal)
  let latest = created
  let done = false
  for await (const event of await api.events(input.tenantId, created.id, signal)) {
    onEvent(event)
    if (event.type === 'error') throw apiError(0, event.code, event.title)
    if (event.type === 'result') latest = event.run
    if (event.type === 'done') done = true
  }
  if (!done) throw apiError(0, 'stream_incomplete', '合同审查进度流未正常结束')
  if (!isCompletedReview(latest)) latest = await api.get(input.tenantId, created.id, signal)
  if (!isCompletedReview(latest)) throw apiError(0, 'review_incomplete', '合同审查尚未完成')
  return { run: latest, pdf: await api.document(input.tenantId, created.id, signal) }
}

export async function runDemoReview(
  api: ContractReviewApi,
  tenantId: string,
  onEvent: (event: ContractReviewEvent) => void,
  signal?: AbortSignal,
): Promise<{ run: ContractReviewRun; pdf: Uint8Array }> {
  let latest: ContractReviewRun | null = null
  let done = false
  for await (const event of await api.demoEvents(tenantId, signal)) {
    onEvent(event)
    if (event.type === 'error') throw apiError(0, event.code, event.title)
    if (event.type === 'result') latest = event.run
    if (event.type === 'done') done = true
  }
  if (!done || !latest || !isCompletedReview(latest) || latest.is_demo !== true) {
    throw apiError(0, 'demo_fixture_unavailable', '演示样例未能完成，请稍后重试')
  }
  return { run: latest, pdf: await api.demoDocument(tenantId, signal) }
}
