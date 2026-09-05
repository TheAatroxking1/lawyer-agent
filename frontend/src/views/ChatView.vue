<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

import { ApiError } from '../api'
import { chatStream } from '../api/sse'
import AuthModal from '../components/AuthModal.vue'
import { authState, refreshAuth } from '../auth/state'
import { session } from '../auth/session'
import { MAX_CONTENT_CHARS, composeChatMessages } from '../chat/messages'
import type { ChatTurn } from '../chat/messages'

const turns = ref<ChatTurn[]>([])
const draft = ref('')
const sending = ref(false)
const error = ref<ApiError | null>(null)
const transcript = ref<HTMLElement | null>(null)
const showLogin = ref(false)
const pendingSend = ref(false)

function guidanceFor(code: string): string | null {
  switch (code) {
    case 'model_provider_unavailable':
      return '模型服务未配置：请复制 deploy/deepseek.config.example.json 为 deploy/deepseek.config.json，填入你的 DeepSeek API Key 后重启后端。'
    case 'authentication_failed':
      return '登录已失效，请重新登录后再发送。'
    default:
      return null
  }
}

function openLogin(): void {
  error.value = null
  showLogin.value = true
}

function onLoggedOut401(): void {
  session.clearToken()
  refreshAuth()
  openLogin()
}

function errorFromEvent(data: Record<string, unknown>): ApiError {
  const status = typeof data['status'] === 'number' ? data['status'] : 0
  const code =
    typeof data['code'] === 'string' && data['code'].length > 0
      ? data['code']
      : 'chat_stream_error'
  const title =
    typeof data['title'] === 'string' && data['title'].length > 0
      ? data['title']
      : '回答未完整生成，请重试。'
  return new ApiError({ status, code, title })
}

function scrollTranscript(): void {
  void nextTick().then(() => {
    if (transcript.value) {
      transcript.value.scrollTop = transcript.value.scrollHeight
    }
  })
}

async function doSend(content: string): Promise<void> {
  const history: ChatTurn[] = [...turns.value, { role: 'user', content }]
  // Reserve the assistant bubble immediately; deltas then render token by token.
  turns.value = [...history, { role: 'assistant', content: '…' }]
  const assistantIndex = turns.value.length - 1
  sending.value = true
  const parts: string[] = []
  let failed: ApiError | null = null
  try {
    const stream = await chatStream(composeChatMessages(history))
    for await (const frame of stream) {
      if (frame.event === 'delta') {
        const text = frame.data['text']
        if (typeof text === 'string' && text.length > 0) parts.push(text)
        turns.value[assistantIndex] = { role: 'assistant', content: parts.join('') }
        scrollTranscript()
      } else if (frame.event === 'error') {
        failed = errorFromEvent(frame.data)
      } else if (frame.event === 'done') {
        break
      }
    }
  } catch (cause) {
    failed =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    sending.value = false
  }
  if (failed) {
    error.value = failed
    if (parts.length === 0) {
      // Nothing was generated: drop the placeholder bubble and keep the banner.
      turns.value = turns.value.filter((_, index) => index !== assistantIndex)
    }
  }
  scrollTranscript()
}

function requestSend(): void {
  if (sending.value) return
  error.value = null
  const content = draft.value.trim()
  if (content.length === 0) return
  if (!authState.authenticated) {
    // Keep the draft in the box; only authenticate when the user actually
    // talks to the AI, then resend the same message.
    pendingSend.value = true
    showLogin.value = true
    return
  }
  draft.value = ''
  void doSend(content)
}

function onModalSuccess(): void {
  showLogin.value = false
  const resend = pendingSend.value
  pendingSend.value = false
  if (resend && authState.authenticated) {
    const content = draft.value.trim()
    if (content.length > 0) {
      draft.value = ''
      void doSend(content)
    }
  }
}

function clearConversation(): void {
  turns.value = []
  error.value = null
}

watch(
  () => turns.value.length,
  async () => {
    await nextTick()
    if (transcript.value) {
      transcript.value.scrollTop = transcript.value.scrollHeight
    }
  },
)
</script>

<template>
  <section class="chat-page">
    <header class="chat-head">
      <div>
        <h1>法规对话</h1>
        <p class="lead">基于 DeepSeek 的公共问答。回答由模型生成，请以现行有效法律法规为准并自行核验。</p>
      </div>
      <button v-if="turns.length > 0" class="ghost" type="button" @click="clearConversation">
        清空对话
      </button>
    </header>

    <div ref="transcript" class="transcript" aria-live="polite">
      <p v-if="turns.length === 0" class="empty">
        开始提问吧，例如：<button class="link" type="button" @click="draft = '承租人逾期支付租金，出租人可以要求支付违约金吗？'; requestSend()">承租人逾期支付租金，出租人可以要求支付违约金吗？</button>
      </p>
      <article v-for="(turn, index) in turns" :key="index" class="bubble" :class="turn.role">
        <div class="who">{{ turn.role === 'user' ? '我' : '律师 Agent' }}</div>
        <div class="body">{{ turn.content }}</div>
      </article>
    </div>

    <div class="compose">
      <p v-if="!authState.authenticated" class="guest-note">
        未登录可浏览与输入；点击发送即弹出登录（支持微信 / 手机号 / 账号密码）。
      </p>
      <p v-if="error" class="error" role="alert">
        <span class="code">{{ error.code }}</span>
        <span>{{ error.title }}</span>
        <button
          v-if="error.code === 'authentication_failed'"
          type="button"
          @click="onLoggedOut401"
        >
          重新登录
        </button>
      </p>
      <p v-if="error && guidanceFor(error.code)" class="guidance">{{ guidanceFor(error.code) }}</p>
      <form class="compose-row" @submit.prevent="requestSend">
        <textarea
          v-model="draft"
          :maxlength="MAX_CONTENT_CHARS"
          rows="3"
          placeholder="输入问题（Enter 发送，Shift+Enter 换行）"
          :disabled="sending"
          @keydown.enter.exact.prevent="requestSend"
        ></textarea>
        <button
          type="submit"
          :disabled="sending || draft.trim().length === 0"
          :title="authState.authenticated ? undefined : '登录后可发送给 AI'"
        >
          {{ sending ? '发送中…' : authState.authenticated ? '发送' : '登录并发送' }}
        </button>
      </form>
    </div>

    <AuthModal v-if="showLogin" @close="showLogin = false" @success="onModalSuccess" />
  </section>
</template>

<style scoped>
.chat-page {
  display: grid;
  grid-template-rows: auto 1fr auto;
  gap: 0.9rem;
  height: calc(100vh - 8.5rem);
}
.chat-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}
.chat-head h1 {
  margin: 0;
}
.lead {
  margin: 0.3rem 0 0;
  color: var(--color-text-muted);
  font-size: 0.88rem;
  max-width: 44rem;
}
.ghost {
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text-secondary);
}
.transcript {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
  padding: 0.4rem;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-surface);
}
.empty {
  margin: auto;
  color: var(--color-text-muted);
  text-align: center;
}
.link {
  border: none;
  background: none;
  color: var(--color-accent);
  padding: 0;
  text-decoration: underline;
  cursor: pointer;
  font-size: inherit;
}
.bubble {
  display: grid;
  gap: 0.25rem;
  max-width: 78%;
}
.bubble.user {
  align-self: flex-end;
}
.bubble.assistant {
  align-self: flex-start;
}
.who {
  font-size: 0.76rem;
  color: var(--color-text-muted);
}
.bubble.user .who {
  text-align: right;
}
.body {
  padding: 0.6rem 0.85rem;
  border-radius: 10px;
  font-size: 0.95rem;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble.user .body {
  background: var(--color-accent);
  color: #ffffff;
  border-bottom-right-radius: 2px;
}
.bubble.assistant .body {
  background: var(--color-bg-subtle);
  border-bottom-left-radius: 2px;
}
.thinking {
  color: var(--color-text-muted);
  font-size: 0.9rem;
}
.compose {
  display: grid;
  gap: 0.4rem;
}
.error {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
  margin: 0;
  padding: 0.5rem 0.7rem;
  border: 1px solid var(--color-danger-border);
  background: var(--color-danger-bg);
  color: var(--color-danger-text);
  border-radius: 6px;
  font-size: 0.88rem;
}
.code {
  font-family: var(--font-mono);
  font-size: 0.78rem;
}
.error button {
  margin-left: auto;
  border-color: var(--color-danger-text);
  background: transparent;
  color: var(--color-danger-text);
  padding: 0.15rem 0.6rem;
  font-size: 0.82rem;
}
.guidance {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 0.85rem;
}
.usage {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 0.78rem;
}
.guest-note {
  margin: 0;
  padding: 0.4rem 0.7rem;
  border: 1px dashed var(--color-accent-soft-border);
  border-radius: 6px;
  background: var(--color-accent-soft);
  color: var(--color-text-secondary);
  font-size: 0.85rem;
}
.compose-row {
  display: flex;
  gap: 0.6rem;
  align-items: flex-end;
}
.compose-row textarea {
  flex: 1;
  resize: vertical;
  min-height: 3.5rem;
  max-height: 12rem;
}
</style>
