<script setup lang="ts">
import { ref } from 'vue'

import { ApiError } from '../api'
import type { RetrievalQuestionReply } from '../api'
import { askQuestionStream } from '../api/sse'
import AuthModal from '../components/AuthModal.vue'
import { authState, refreshAuth } from '../auth/state'
import { session } from '../auth/session'

const question = ref('')
const busy = ref(false)
const phase = ref<'idle' | 'searching'>('idle')
const error = ref<ApiError | null>(null)
const reply = ref<RetrievalQuestionReply | null>(null)
const showLogin = ref(false)
const pendingAsk = ref(false)

function guidanceFor(code: string): string | null {
  switch (code) {
    case 'retrieval_qa_unavailable':
      return '有据问答暂不可用：需要配置 DeepSeek API Key、OpenSearch 在线且已发布数据集（dataset_v1）。'
    case 'legal_dataset_not_published':
      return '当前数据集尚未发布到检索别名，暂无法回答。'
    case 'model_provider_unavailable':
      return '模型服务未就绪：请确认 DeepSeek API Key（deploy/deepseek.config.json）或本地 Embedding 模型可用。'
    case 'model_provider_timeout':
      return '模型调用超时，请稍后重试。'
    default:
      return null
  }
}

function openLogin(): void {
  error.value = null
  showLogin.value = true
}

function on401(): void {
  session.clearToken()
  session.clearTenantToken()
  session.clearActiveTenant()
  refreshAuth()
  openLogin()
}

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = null
  const content = question.value.trim()
  if (content.length === 0) return
  if (!authState.authenticated) {
    pendingAsk.value = true
    showLogin.value = true
    return
  }
  await ask(content)
}

async function ask(content: string): Promise<void> {
  reply.value = null
  error.value = null
  busy.value = true
  phase.value = 'searching'
  try {
    const events = await askQuestionStream({
      question: content,
      target_date: new Date().toISOString().slice(0, 10),
    })
    for await (const event of events) {
      if (event.event === 'answer') {
        reply.value = event.data as unknown as RetrievalQuestionReply
      } else if (event.event === 'error') {
        const failure = event.data as unknown as {
          status?: number
          code?: string
          title?: string
        }
        error.value = new ApiError({
          status: failure.status ?? 502,
          code: failure.code ?? 'retrieval_failed',
          title: failure.title ?? '检索问答失败',
        })
      } else if (event.event === 'done') {
        break
      }
    }
  } catch (cause) {
    error.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    busy.value = false
    phase.value = 'idle'
  }
}

function onModalSuccess(): void {
  showLogin.value = false
  const resend = pendingAsk.value
  pendingAsk.value = false
  if (resend && authState.authenticated) {
    const content = question.value.trim()
    if (content.length > 0) void ask(content)
  }
}

function newQuestion(): void {
  reply.value = null
  error.value = null
}
</script>

<template>
  <section class="ask-page">
    <header class="head">
      <div>
        <h1>法规问答（有据）</h1>
        <p class="lead">
          基于已发布法规数据集的检索问答：结论必须引用权威条文（证据门禁），无法核验时安全拒答。
        </p>
      </div>
    </header>

    <form class="ask-box" @submit.prevent="submit">
      <textarea
        v-model="question"
        :maxlength="4000"
        rows="3"
        placeholder="输入问题，例如：承租人逾期支付租金，出租人可以要求支付违约金吗？"
        :disabled="busy"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <div class="row">
        <button
          type="submit"
          :disabled="busy || question.trim().length === 0"
          :title="authState.authenticated ? undefined : '登录后可提交'"
        >
          {{ busy ? '检索中…' : authState.authenticated ? '提问' : '登录并提问' }}
        </button>
        <button v-if="reply" class="ghost" type="button" @click="newQuestion">重新提问</button>
      </div>
    </form>

    <p v-if="!authState.authenticated" class="guest-note">
      未登录可输入问题；提交时弹出登录（微信 / 手机号 / 账号密码）。
    </p>

    <p v-if="error" class="error" role="alert">
      <span class="code">{{ error.code }}</span>
      <span>{{ error.title }}</span>
      <button v-if="error.code === 'authentication_failed'" type="button" @click="on401">重新登录</button>
    </p>
    <p v-if="error && guidanceFor(error.code)" class="guidance">{{ guidanceFor(error.code) }}</p>

    <section v-if="busy" class="thinking">正在检索权威条文并生成回答…</section>

    <section v-if="reply && reply.refused" class="refused" aria-live="polite">
      <p class="refused-tag">已安全拒答 · {{ reply.reason }}</p>
      <p class="refused-text">{{ reply.text }}</p>
    </section>

    <section v-if="reply && !reply.refused" class="answer" aria-live="polite">
      <div class="answer-text">{{ reply.text }}</div>
      <div class="citation-head">
        <h2>引用依据</h2>
        <span v-if="reply.usage" class="usage">
          tokens：{{ reply.usage.completion_tokens }}（累计 {{ reply.usage.total_tokens }}）
        </span>
      </div>
      <ul v-if="reply.citations.length > 0" class="citations">
        <li v-for="citation in reply.citations" :key="citation.evidence_id" class="citation">
          <p class="cite-title">
            {{ citation.instrument_title }} · {{ citation.version_label }} ·
            {{ citation.provision_no }}
          </p>
          <p class="cite-text">{{ citation.provision_text }}</p>
          <p class="cite-meta">
            数据集 {{ citation.dataset_version }} · 来源 {{ citation.source_ref }}
          </p>
        </li>
      </ul>
      <p v-else class="no-citations">回答通过核验但未附独立引用条目。</p>
    </section>

    <AuthModal v-if="showLogin" @close="showLogin = false" @success="onModalSuccess" />
  </section>
</template>

<style scoped>
.ask-page {
  display: grid;
  gap: 1rem;
  max-width: 52rem;
}
.head h1 {
  margin: 0;
}
.lead {
  margin: 0.3rem 0 0;
  color: var(--color-text-muted);
  font-size: 0.88rem;
}
.ask-box {
  display: grid;
  gap: 0.6rem;
}
.ask-box textarea {
  resize: vertical;
  min-height: 5rem;
}
.row {
  display: flex;
  gap: 0.6rem;
  align-items: center;
}
.ghost {
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text-secondary);
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
.thinking {
  color: var(--color-text-muted);
  font-size: 0.9rem;
}
.refused {
  padding: 0.9rem 1rem;
  border: 1px dashed var(--color-danger-border);
  border-radius: 8px;
  background: var(--color-danger-bg);
}
.refused-tag {
  margin: 0 0 0.3rem;
  font-weight: 600;
  color: var(--color-danger-text);
  font-size: 0.85rem;
}
.refused-text {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.7;
}
.answer {
  display: grid;
  gap: 1rem;
}
.answer-text {
  white-space: pre-wrap;
  line-height: 1.8;
  font-size: 0.98rem;
}
.citation-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.citation-head h2 {
  margin: 0;
  font-size: 1.02rem;
}
.usage {
  color: var(--color-text-muted);
  font-size: 0.78rem;
}
.citations {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.7rem;
}
.citation {
  border: 1px solid var(--color-border);
  border-left: 4px solid var(--color-accent);
  border-radius: 8px;
  padding: 0.8rem 1rem;
  background: var(--color-surface);
}
.cite-title {
  margin: 0 0 0.4rem;
  font-weight: 600;
  font-size: 0.94rem;
}
.cite-text {
  margin: 0 0 0.4rem;
  white-space: pre-wrap;
  line-height: 1.7;
  font-size: 0.9rem;
  color: var(--color-text-secondary);
}
.cite-meta {
  margin: 0;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  font-size: 0.76rem;
  word-break: break-all;
}
.no-citations {
  color: var(--color-text-muted);
  font-size: 0.88rem;
}
</style>
