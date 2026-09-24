import { tenantApiClient } from './tenant'
import { tenantEventStream } from './sse'

export interface ConversationSummary {
  id: string
  title: string
  updated_at: string
  review_id?: string | null
}
export interface SavedMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  status: 'streaming' | 'completed' | 'incomplete'
}
export interface ConversationDetail extends ConversationSummary {
  messages: SavedMessage[]
  has_more?: boolean
  next_message_cursor?: string | null
}
export interface ReviewHistoryItem {
  id: string
  file_name: string
  status: string
  created_at: string
  updated_at: string
}
export interface HistoryPage<T> { items: T[]; next_cursor: string | null }

function prefix(tenantId: string): string { return `/tenants/${encodeURIComponent(tenantId)}` }
function pageQuery(cursor?: string | null): string {
  return `?limit=30${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`
}
export function listConversations(tenantId: string, cursor?: string | null) {
  return tenantApiClient.request<HistoryPage<ConversationSummary>>(`${prefix(tenantId)}/conversations${pageQuery(cursor)}`)
}
export function listReviewHistory(tenantId: string, cursor?: string | null) {
  return tenantApiClient.request<HistoryPage<ReviewHistoryItem>>(`${prefix(tenantId)}/contract-reviews${pageQuery(cursor)}`)
}
export function createConversation(tenantId: string, title: string, reviewId?: string | null) {
  return tenantApiClient.request<ConversationSummary>(`${prefix(tenantId)}/conversations`, {
    method: 'POST',
    body: { title: title.slice(0, 80), ...(reviewId ? { review_id: reviewId } : {}) },
  })
}
export function getConversation(tenantId: string, id: string, cursor?: string | null) {
  const query = cursor ? `?message_cursor=${encodeURIComponent(cursor)}` : ''
  return tenantApiClient.request<ConversationDetail>(`${prefix(tenantId)}/conversations/${encodeURIComponent(id)}${query}`)
}
export function sendConversationMessage(tenantId: string, id: string, content: string, signal?: AbortSignal) {
  return tenantEventStream(`${prefix(tenantId)}/conversations/${encodeURIComponent(id)}/messages/stream`, { content, request_id: crypto.randomUUID() }, signal)
}
