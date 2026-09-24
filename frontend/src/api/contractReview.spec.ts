import { describe, expect, it, vi } from 'vitest'

import {
  createContractReviewApi,
  createAsyncGenerationGuard,
  createReviewRequestGuard,
  boundedCanvasScale,
  isCompletedReview,
  overlayPercent,
  runContractReview,
  runDemoReview,
  readPdfPreview,
  type ContractReviewEvent,
  type ContractReviewRun,
} from './contractReview'

const run: ContractReviewRun = {
  id: 'run-1',
  status: 'running',
  file_name: '合同.pdf',
  page_dimensions: [[595, 842]],
  issues: [],
  blocks: [],
}

function response(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  })
}

describe('contract review API', () => {
  it('runs the deterministic interview demo without upload or model calls', async () => {
    const encoder = new TextEncoder()
    const calls: string[] = []
    const fetchImpl = vi.fn(async (url: string) => {
      calls.push(url)
      if (url.endsWith('/demo/events')) {
        return new Response(new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode([
              'event: progress\ndata: {"stage":"researching"}\n\n',
              'event: result\ndata: {"id":"demo","status":"draft","file_name":"演示合同.pdf","page_dimensions":[[612,792]],"issues":[],"blocks":[],"is_demo":true}\n\n',
              'event: done\ndata: {"status":"draft"}\n\n',
            ].join('')))
            controller.close()
          },
        }), { status: 200 })
      }
      return new Response(new Uint8Array([37, 80, 68, 70]), { status: 200 })
    })
    const api = createContractReviewApi({ baseUrl: '/api/v1', fetchImpl })
    const result = await runDemoReview(api, 'tenant-a', () => undefined)
    expect(result.run.is_demo).toBe(true)
    expect([...result.pdf]).toEqual([37, 80, 68, 70])
    expect(calls).toEqual([
      '/api/v1/tenants/tenant-a/contract-reviews/demo/events',
      '/api/v1/tenants/tenant-a/contract-reviews/demo/document',
    ])
  })

  it('creates, uploads raw PDF, streams fixed events and downloads with tenant auth', async () => {
    const encoder = new TextEncoder()
    const events = [
      'event: progress\ndata: {"stage":"content_warning"}\n\n',
      'event: progress\ndata: {"stage":"contract_understanding"}\n\n',
      'event: result\ndata: {"id":"run-1","status":"draft","file_name":"合同.pdf","page_dimensions":[[595,842]],"issues":[],"blocks":[],"verification_report":{"pages":[{"page":1,"reasons":["missing_text"]}]}}\n\n',
      'event: done\ndata: {"status":"draft"}\n\n',
    ].join('')
    const calls: Array<{ url: string; init: RequestInit }> = []
    const fetchImpl = vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      if (calls.length === 1) return response(run, { status: 201 })
      if (calls.length === 2) return response({ ...run, status: 'uploaded' })
      if (calls.length === 3) {
        return new Response(new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(events))
            controller.close()
          },
        }), { status: 200, headers: { 'content-type': 'text/event-stream' } })
      }
      return new Response(new Uint8Array([37, 80, 68, 70]), {
        status: 200,
        headers: { 'content-type': 'application/pdf' },
      })
    })
    const api = createContractReviewApi({
      baseUrl: '/api/v1',
      tokenProvider: () => 'tenant-token',
      fetchImpl,
      idFactory: () => 'idem-1',
    })
    const seen: ContractReviewEvent[] = []
    const result = await runContractReview(
      api,
      { tenantId: 'tenant-a', file: new File([new Uint8Array([1, 2])], '合同.pdf', { type: 'application/pdf' }) },
      (event) => seen.push(event),
    )

    expect(result.run.status).toBe('draft')
    expect([...result.pdf]).toEqual([37, 80, 68, 70])
    expect(seen.map((event) => event.type)).toEqual(['progress', 'progress', 'result', 'done'])
    expect(result.run.verification_report?.pages).toEqual([{ page: 1, reasons: ['missing_text'] }])
    expect(calls.map((call) => [call.init.method, call.url])).toEqual([
      ['POST', '/api/v1/tenants/tenant-a/contract-reviews'],
      ['PUT', '/api/v1/tenants/tenant-a/contract-reviews/run-1/document'],
      ['POST', '/api/v1/tenants/tenant-a/contract-reviews/run-1/events'],
      ['GET', '/api/v1/tenants/tenant-a/contract-reviews/run-1/document'],
    ])
    expect(calls[0].init.headers).toMatchObject({
      Authorization: 'Bearer tenant-token',
      'Idempotency-Key': 'idem-1',
    })
    expect(calls[1].init.body).toBeInstanceOf(ArrayBuffer)
  })

  it('uses backend default instruction and rejects non-PDF before requests', async () => {
    const fetchImpl = vi.fn()
    const api = createContractReviewApi({ baseUrl: '/api/v1', fetchImpl })
    await expect(runContractReview(
      api,
      { tenantId: 'tenant-a', file: new File(['word'], '合同.docx') },
      () => undefined,
    )).rejects.toMatchObject({ code: 'pdf_required' })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('propagates cancellation and never accepts raw thought events', async () => {
    const controller = new AbortController()
    const api = createContractReviewApi({
      baseUrl: '/api/v1',
      fetchImpl: vi.fn(async (_url, init) => {
        controller.abort()
        throw init.signal?.reason ?? new DOMException('Aborted', 'AbortError')
      }),
    })
    await expect(runContractReview(
      api,
      { tenantId: 'tenant-a', file: new File(['%PDF'], '合同.pdf', { type: 'application/pdf' }) },
      () => undefined,
      controller.signal,
    )).rejects.toMatchObject({ name: 'AbortError' })

    expect(() => isCompletedReview({ ...run, status: 'running' })).not.toThrow()
    expect(isCompletedReview({ ...run, status: 'running' })).toBe(false)
    expect(isCompletedReview({ ...run, status: 'draft' })).toBe(true)
    expect(isCompletedReview({ ...run, status: 'no_evidence' })).toBe(true)
  })

  it('rejects rawThought and reads the current tenant token per request', async () => {
    const encoder = new TextEncoder()
    let token = 'tenant-a-token'
    const calls: RequestInit[] = []
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      calls.push(init)
      if (calls.length <= 2) return response(run)
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode('event: rawThought\ndata: {"text":"private"}\n\n'))
          controller.close()
        },
      }), { status: 200 })
    })
    const api = createContractReviewApi({
      baseUrl: '/api/v1',
      tokenProvider: () => token,
      fetchImpl,
    })
    await api.create('tenant-a', { file_name: 'a.pdf' })
    token = 'tenant-b-token'
    await api.create('tenant-b', { file_name: 'b.pdf' })
    expect(calls[0].headers).toMatchObject({ Authorization: 'Bearer tenant-a-token' })
    expect(calls[1].headers).toMatchObject({ Authorization: 'Bearer tenant-b-token' })
    const events = await api.events('tenant-b', 'run-1')
    await expect((async () => {
      for await (const event of events) void event
    })()).rejects.toMatchObject({ code: 'invalid_event' })
  })

  it('rejects a late old-tenant completion even when abort is ignored', () => {
    let token: string | null = 'tenant-a-token'
    const guard = createReviewRequestGuard(() => token)
    const oldRun = guard.begin('tenant-a')
    token = 'tenant-b-token'
    const currentRun = guard.begin('tenant-b')

    expect(guard.isCurrent(oldRun, 'tenant-b')).toBe(false)
    expect(guard.isCurrent(currentRun, 'tenant-b')).toBe(true)
    token = 'rotated-tenant-b-token'
    expect(guard.isCurrent(currentRun, 'tenant-b')).toBe(false)
  })

  it('rejects a tenant-switch response after logout or component teardown', () => {
    let accountToken: string | null = 'account-token'
    const guard = createReviewRequestGuard(() => accountToken)
    const switchIdentity = guard.begin('tenant-a')
    accountToken = null
    expect(guard.isCurrent(switchIdentity, 'tenant-a')).toBe(false)

    accountToken = 'new-account-token'
    const newIdentity = guard.begin('tenant-b')
    guard.invalidate()
    expect(guard.isCurrent(newIdentity, 'tenant-b')).toBe(false)
  })

  it('reads a bounded local PDF preview before network review', async () => {
    const file = new File([new Uint8Array([37, 80, 68, 70])], 'local.pdf', {
      type: 'application/pdf',
    })
    expect([...await readPdfPreview(file, 16)]).toEqual([37, 80, 68, 70])
    await expect(readPdfPreview(file, 3)).rejects.toMatchObject({ code: 'file_too_large' })
  })

  it('bounds PDF canvas scale by viewport, edge and area limits', () => {
    expect(boundedCanvasScale(595, 842)).toBeCloseTo(780 / 595)
    const scale = boundedCanvasScale(20_000, 20_000)
    expect(20_000 * scale).toBeLessThanOrEqual(4096)
    expect(20_000 * scale * 20_000 * scale).toBeLessThanOrEqual(12_000_000)
    expect(() => boundedCanvasScale(Number.NaN, 100)).toThrow('invalid_pdf_page')
  })

  it('positions overlays proportionally when the rendered canvas is narrowed', () => {
    expect(overlayPercent(100, 200, 50, 40, 500, 1000)).toEqual({
      left: '20%', top: '20%', width: '10%', height: '4%',
    })
    expect(() => overlayPercent(0, 0, 1, 1, 0, 100)).toThrow('invalid_overlay_geometry')
  })

  it('invalidates an older PDF load before its delayed cleanup completes', async () => {
    const guard = createAsyncGenerationGuard()
    let release!: () => void
    const delayedCleanup = new Promise<void>((resolve) => { release = resolve })
    const oldIdentity = guard.begin()
    const oldLoad = (async () => {
      await delayedCleanup
      return guard.isCurrent(oldIdentity)
    })()
    const currentIdentity = guard.begin()
    expect(guard.isCurrent(currentIdentity)).toBe(true)
    release()
    expect(await oldLoad).toBe(false)
    guard.invalidate()
    expect(guard.isCurrent(currentIdentity)).toBe(false)
  })
})
