<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { listConversations, listReviewHistory, type ConversationSummary, type ReviewHistoryItem } from '../api/conversations'
import { historyRevision } from '../chat/history'

const props = defineProps<{ tenantId: string; identity: string }>()
const emit = defineEmits<{ navigate: [] }>()
const chats = ref<ConversationSummary[]>([])
const reviews = ref<ReviewHistoryItem[]>([])
const chatCursor = ref<string | null>(null)
const reviewCursor = ref<string | null>(null)
const loading = ref(false)
const failed = ref(false)
let generation = 0
const rows = computed(() => {
  const linkedReviews = new Set(chats.value.flatMap(item => item.review_id ? [item.review_id] : []))
  return [
    ...chats.value.map(item => ({
      id: item.id,
      reviewId: item.review_id ?? null,
      title: item.title,
      updated: item.updated_at,
      kind: 'conversation',
      label: item.review_id ? '合同对话' : '对话',
    })),
    ...reviews.value.filter(item => !linkedReviews.has(item.id)).map(item => ({
      id: item.id,
      reviewId: null,
      title: item.file_name,
      updated: item.updated_at,
      kind: 'review',
      label: item.status === 'draft' ? '审阅草稿' : item.status === 'failed' ? '审阅未完成' : '合同审阅',
    })),
  ].sort((a, b) => b.updated.localeCompare(a.updated))
})

async function load(more = false): Promise<void> {
  const ticket = ++generation
  if (!props.tenantId || !props.identity) { loading.value = false; return }
  loading.value = true
  failed.value = false
  try {
    const [chatPage, reviewPage] = await Promise.all([
      !more || chatCursor.value ? listConversations(props.tenantId, more ? chatCursor.value : null) : null,
      !more || reviewCursor.value ? listReviewHistory(props.tenantId, more ? reviewCursor.value : null) : null,
    ])
    if (ticket !== generation) return
    if (chatPage) {
      chats.value = more ? [...chats.value, ...chatPage.items] : chatPage.items
      chatCursor.value = chatPage.next_cursor
    }
    if (reviewPage) {
      reviews.value = more ? [...reviews.value, ...reviewPage.items] : reviewPage.items
      reviewCursor.value = reviewPage.next_cursor
    }
  } catch { if (ticket === generation) failed.value = true }
  finally { if (ticket === generation) loading.value = false }
}

watch(() => [props.tenantId, props.identity], () => {
  chats.value = []; reviews.value = []; chatCursor.value = null; reviewCursor.value = null
  void load()
}, { immediate: true })
watch(historyRevision, () => { void load() })
onBeforeUnmount(() => { generation += 1 })
</script>

<template>
  <section class="history" aria-label="历史会话">
    <h2>历史会话</h2>
    <p v-if="!identity">登录后查看历史会话</p>
    <p v-else-if="!tenantId">选择工作空间后查看历史</p>
    <p v-else-if="loading && !rows.length" role="status">正在加载…</p>
    <div v-if="failed" class="history-error"><p>历史记录加载失败</p><button @click="load()">重试</button></div>
    <p v-else-if="tenantId && !loading && !rows.length">还没有会话，发送消息即可开始</p>
    <nav aria-label="已保存的会话">
      <RouterLink v-for="row in rows" :key="row.kind + row.id" :to="{ path: '/chat', query: { tenant: tenantId, [row.kind]: row.id, ...(row.reviewId ? { review: row.reviewId } : {}) } }" :title="row.title" @click="emit('navigate')">
        <span aria-hidden="true">{{ row.kind === 'review' ? '▤' : '◌' }}</span>
        <div><strong>{{ row.title || '新对话' }}</strong><small>{{ row.label }}</small></div>
      </RouterLink>
    </nav>
    <button v-if="chatCursor || reviewCursor" :disabled="loading" class="history-more" @click="load(true)">{{ loading ? '加载中…' : '加载更早记录' }}</button>
  </section>
</template>

<style scoped>
.history { flex: 1; min-height: 100px; overflow-y: auto; margin: 18px -5px 10px; padding: 0 5px; }
h2 { margin: 0 10px 8px; font-size: 12px; font-weight: 500; color: #777971; }
p { margin: 12px 10px; color: #7b7e77; font-size: 12px; line-height: 1.6; }
nav { display: grid; gap: 3px; }
nav a { display: flex; gap: 9px; padding: 9px 10px; border-radius: 9px; color: #3b3b37; text-decoration: none; min-width: 0; }
nav a:hover, nav a.router-link-exact-active { background: #e7e9e3; }
nav a > span { color: #727a6e; font-size: 17px; }
nav a > div { min-width: 0; }
strong { display: block; font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
small { display: block; margin-top: 3px; font-size: 10px; color: #8b8d85; }
button { font-size: 12px; padding: 6px 10px; border: 1px solid #d7dbd1; background: transparent; color: #4f584a; border-radius: 6px; }
.history-more { width: 100%; margin-top: 8px; }
.history-error { display: flex; align-items: center; }
</style>
