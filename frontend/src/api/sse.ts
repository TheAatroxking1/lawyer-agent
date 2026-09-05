// Consumes the retrieval Q&A SSE endpoint
// (POST /api/v1/legal/questions/stream) over fetch ReadableStream.
//
// The transport frames are stable: started -> answer|error -> done. JSON data
// is parsed per event; non-2xx responses are mapped to ApiError just like the
// JSON endpoints.

import { ApiError } from './client'
import { session } from '../auth/session'
import { parseSseBlock } from '../lib/sse'
import type { ApiProblemBody, RetrievalQuestionInput } from './types'

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

export async function askQuestionStream(
  input: RetrievalQuestionInput,
  signal?: AbortSignal,
): Promise<AsyncIterable<AskStreamEvent>> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = session.readToken()
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(`${apiBase}/legal/questions/stream`, {
    method: 'POST',
    headers,
    body: JSON.stringify(input),
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
