// Consumes the SSE chat/Q&A endpoints
// (POST /api/v1/legal/questions/stream, POST /api/v1/legal/chat/stream) over a
// fetch ReadableStream.
//
// The transport frames are stable: started -> answer|delta*|error -> done.
// JSON data is parsed per event; non-2xx responses are mapped to ApiError just
// like the JSON endpoints.

import { ApiError } from './client'
import { authenticatedFetch } from '../auth/transport'
import { parseSseBlock } from '../lib/sse'
import type { ApiProblemBody, ChatMessageInput, RetrievalQuestionInput } from './types'

const apiBase: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1'

export interface AskStreamEvent {
  event: string
  data: Record<string, unknown>
}

async function readErrorBody(response: Response): Promise<ApiError> {
  let problem: ApiProblemBody | null = null
  try {
    const body = (await response.json()) as ApiProblemBody
    if (body && typeof body.code === 'string' && typeof body.title === 'string') {
      problem = body
    }
  } catch {
    problem = null
  }
  if (problem) {
    return new ApiError({
      status: problem.status ?? response.status,
      code: problem.code ?? `http_${response.status}`,
      title: problem.title ?? 'Request failed',
      headers: response.headers,
    })
  }
  return new ApiError({
    status: response.status,
    code: `http_${response.status}`,
    title: `HTTP ${response.status}`,
    headers: response.headers,
  })
}

/**
 * Opens one SSE POST and yields its frames until the transport closes.
 * Throws ApiError before yielding when the response is not 2xx.
 */
export async function postEventStream(
  path: string,
  payload: unknown,
  signal?: AbortSignal,
  scope: 'account' | 'tenant' = 'account',
): Promise<AsyncIterable<AskStreamEvent>> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const response = await authenticatedFetch(scope, `${apiBase}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
    signal,
  })
  if (!response.ok || !response.body) {
    if (!response.ok) throw await readErrorBody(response)
    throw new ApiError({
      status: response.status,
      code: 'http_0',
      title: 'Stream unavailable',
    })
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  async function* events(): AsyncIterable<AskStreamEvent> {
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let boundary = buffer.indexOf('\n\n')
        while (boundary >= 0) {
          const block = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          const frame = parseSseBlock(block)
          if (frame.event && frame.data !== undefined) {
            yield {
              event: frame.event,
              data: JSON.parse(frame.data) as Record<string, unknown>,
            }
          }
          boundary = buffer.indexOf('\n\n')
        }
      }
    } finally {
      reader.releaseLock()
    }
  }
  return events()
}

export function tenantEventStream(path: string, payload: unknown, signal?: AbortSignal): Promise<AsyncIterable<AskStreamEvent>> {
  return postEventStream(path, payload, signal, 'tenant')
}

export function askQuestionStream(
  input: RetrievalQuestionInput,
  signal?: AbortSignal,
): Promise<AsyncIterable<AskStreamEvent>> {
  return postEventStream('/legal/questions/stream', input, signal)
}

export function chatStream(
  messages: ChatMessageInput[],
  signal?: AbortSignal,
): Promise<AsyncIterable<AskStreamEvent>> {
  return postEventStream('/legal/chat/stream', { messages }, signal)
}
