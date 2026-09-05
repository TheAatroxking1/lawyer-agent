// Pure helpers for building the legal chat request from the UI conversation.

import type { ChatMessageInput } from '../api/types'

export interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
}

export const MAX_TURNS = 12
export const MAX_CONTENT_CHARS = 4000

/**
 * Keeps the most recent `maxTurns` turns (pairs stay intact because turns are
 * counted, not messages) and maps them to the backend message shape. Blank
 * turns are dropped; user text is never truncated or rewritten here — the
 * backend validates length limits and reports a stable 422 if exceeded.
 */
export function composeChatMessages(
  turns: readonly ChatTurn[],
  maxTurns: number = MAX_TURNS,
): ChatMessageInput[] {
  const bounded = turns.slice(-Math.max(1, maxTurns))
  const messages: ChatMessageInput[] = []
  for (const turn of bounded) {
    if (typeof turn.content !== 'string' || turn.content.trim().length === 0) {
      continue
    }
    messages.push({ role: turn.role, content: turn.content })
  }
  return messages
}
