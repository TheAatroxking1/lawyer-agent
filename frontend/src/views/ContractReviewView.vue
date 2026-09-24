<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError, apiClient, myTenants } from '../api'
import type { AccountTenant } from '../api'
import {
  createContractReviewApi,
  createReviewRequestGuard,
  MAX_PDF_BYTES,
  readPdfPreview,
  runDemoReview,
  runContractReview,
  type ContractReviewRun,
  type ReviewStage,
} from '../api/contractReview'
import { session } from '../auth/session'
import { authState, refreshAuth } from '../auth/state'
import { ensureSession, selectTenant } from '../auth/transport'
import { createConversation, getConversation, sendConversationMessage } from '../api/conversations'
import { notifyHistoryChanged } from '../chat/history'
import { MAX_CONTENT_CHARS, type ChatTurn } from '../chat/messages'
import AuthModal from '../components/AuthModal.vue'
import ErrorNote from '../components/ErrorNote.vue'
import PdfContractReview from '../components/PdfContractReview.vue'

const route = useRoute()
const router = useRouter()
const conversationId = ref<string | null>(null)
const earlierCursor = ref<string | null>(null)
const loadingEarlier = ref(false)
let loadedHistory = ''

const STAGE_LABELS: Record<ReviewStage, string> = {
  parsing: 'Qwen 正在阅读合同页面图片',
  content_warning: '内容存在核对提醒，继续完成审阅',
  preparing: '准备合同原件',
  researching: '准备法律研究',
  contract_understanding: '理解合同与交易事实',
  legal_search: '检索相关法律文档',
  legal_selection: '筛选直接相关依据',
  legal_read: '读取相关法律段落',
  reviewing: '生成审查草稿',
  validating: '核对原文、坐标与引用',
  saved: '保存审查草稿',
  no_evidence: '未找到相关法律文档',
}

const tenants = ref<AccountTenant[]>([])
const activeTenant = ref<AccountTenant | null>(null)
const loadingTenants = ref(true)
const switching = ref(false)
const instruction = ref('')
const asOf = ref('')
const busy = ref(false)
const stages = ref<ReviewStage[]>([])
const run = ref<ContractReviewRun | null>(null)
const verificationWarnings = computed(() => {
  const labels: Record<string, string> = {
    missing_native_coordinates: '缺少原生文字坐标', unreadable: '部分内容无法辨认',
    missing_text: '疑似缺字', garbled_text: '疑似乱码',
    text_mismatch: '文字存在差异', unconfirmed_coverage: '内容完整性待核对',
    visual_read_failed: '视觉读取未完成，仅保留已有文字，该页需人工核对',
  }
  return (run.value?.verification_report?.pages ?? []).map(
    (item) => `第${item.page}页：${item.reasons.map((reason) => labels[reason] ?? '内容待核对').join('、')}`,
  )
})
const pdfBytes = ref<Uint8Array | null>(null)
const error = ref<ApiError | null>(null)
const inputKey = ref(0)
const fileInput = ref<HTMLInputElement | null>(null)
const transcriptElement = ref<HTMLElement | null>(null)
const followTail = ref(true)
const fileName = ref('')
const pendingFile = ref<File | null>(null)
const pendingDemo = ref(false)
const submittedInstruction = ref('')
type DisplayTurn = ChatTurn & { status?: 'streaming' | 'completed' | 'incomplete' }
const turns = ref<DisplayTurn[]>([])
const showLogin = ref(false)
const pendingSend = ref(false)
const choosingTenant = ref(false)
const showPreview = ref(true)
const showToolCatalog = ref(false)
const dragging = ref(false)
let dragDepth = 0
const hasConversation = computed(() => !!fileName.value || turns.value.length > 0)
const availableTenants = computed(() => tenants.value.filter(t => t.membership_status === 'active' && t.tenant_status === 'active'))
const latestStage = computed(() => stages.value.length ? STAGE_LABELS[stages.value[stages.value.length - 1]!] : '准备审阅合同')
let activeRequest: AbortController | null = null
const requestGuard = createReviewRequestGuard(() => session.tenantIdentity())
const tenantSwitchGuard = createReviewRequestGuard(() => session.accountIdentity())
const chatGuard = createReviewRequestGuard(() => session.tenantIdentity())
let mounted = true

function safeError(cause: unknown, title: string): ApiError {
  return cause instanceof ApiError
    ? cause
    : new ApiError({ status: 0, code: 'network_error', title })
}

function clearPrivateState(): void {
  loadedHistory = ''
  requestGuard.invalidate()
  chatGuard.invalidate()
  activeRequest?.abort()
  activeRequest = null
  busy.value = false
  stages.value = []
  run.value = null
  pdfBytes.value = null
  error.value = null
  instruction.value = ''
  asOf.value = ''
  inputKey.value += 1
  fileName.value = ''
  submittedInstruction.value = ''
  pendingFile.value = null
  pendingDemo.value = false
  turns.value = []
  pendingSend.value = false
  conversationId.value = null
  earlierCursor.value = null
  loadingEarlier.value = false
}

function historyKey(): string {
  return `${String(route.query.tenant ?? '')}/${String(route.query.conversation ?? '')}/${String(route.query.review ?? '')}`
}

function newConversation(): void {
  clearPrivateState()
  loadedHistory = '//'
  void router.replace('/chat')
}

async function restoreHistory(): Promise<void> {
  const key = historyKey()
  if (key === loadedHistory) return
  const tenantId = typeof route.query.tenant === 'string' ? route.query.tenant : ''
  const chatId = typeof route.query.conversation === 'string' ? route.query.conversation : ''
  const reviewId = typeof route.query.review === 'string' ? route.query.review : ''
  if (!chatId && !reviewId) { clearPrivateState(); loadedHistory = key; return }
  clearPrivateState()
  if (!tenantId) {
    error.value = new ApiError({ status: 422, code: 'invalid_history_link', title: '会话链接不完整，请从左侧历史重新打开。' }); return
  }
  const tenant = availableTenants.value.find(item => item.tenant_id === tenantId)
  if (!tenant) { error.value = new ApiError({ status: 404, code: 'resource_unavailable', title: '无法访问此工作空间的会话。' }); return }
  if (activeTenant.value?.tenant_id !== tenantId) await chooseTenant(tenant)
  if (!mounted || key !== historyKey() || activeTenant.value?.tenant_id !== tenantId) return
  clearPrivateState()
  loadedHistory = key
  busy.value = true
  const controller = new AbortController()
  activeRequest = controller
  const identity = requestGuard.begin(tenantId)
  const current = () => mounted && activeRequest === controller && requestGuard.isCurrent(identity, activeTenant.value?.tenant_id ?? null)
  try {
    if (chatId) {
      const saved = await getConversation(tenantId, chatId)
      if (!current()) return
      conversationId.value = saved.id
      earlierCursor.value = saved.next_message_cursor ?? null
      turns.value = saved.messages.map(message => ({ role: message.role, content: message.content, status: message.status === 'completed' ? 'completed' : 'incomplete' }))
    }
    if (reviewId) {
      const api = createContractReviewApi({ baseUrl: (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1', tokenProvider: () => session.readTenantToken() })
      const saved = await api.get(tenantId, reviewId, controller.signal)
      if (!current()) return
      run.value = saved
      fileName.value = saved.file_name
      if (saved.status !== 'created') {
        const bytes = await api.document(tenantId, reviewId, controller.signal)
        if (!current()) return
        pdfBytes.value = bytes
      }
      if (saved.failure_code) error.value = new ApiError({ status: 0, code: saved.failure_code, title: '这是之前未完成的审阅记录。原件与失败状态已保留，可新建对话重新审阅。' })
    }
  } catch (cause) {
    if (current()) { loadedHistory = ''; error.value = safeError(cause, '无法恢复会话，请从左侧重新打开。') }
  } finally {
    if (activeRequest === controller) { activeRequest = null; busy.value = false }
  }
}

async function loadEarlierMessages(): Promise<void> {
  if (!activeTenant.value || !conversationId.value || !earlierCursor.value || loadingEarlier.value) return
  loadingEarlier.value = true
  const tenantId = activeTenant.value.tenant_id
  const id = conversationId.value
  const cursor = earlierCursor.value
  const identity = session.tenantIdentity()
  try {
    const page = await getConversation(tenantId, id, cursor)
    if (!mounted || conversationId.value !== id || session.tenantIdentity() !== identity) return
    const older: DisplayTurn[] = page.messages.map(message => ({ role: message.role, content: message.content, status: message.status === 'completed' ? 'completed' : 'incomplete' }))
    turns.value = [...older, ...turns.value]
    earlierCursor.value = page.next_message_cursor ?? null
  } catch (cause) {
    if (mounted && conversationId.value === id && session.tenantIdentity() === identity) error.value = safeError(cause, '加载早期消息失败，请重试。')
  } finally { if (conversationId.value === id) loadingEarlier.value = false }
}

async function loadTenants(): Promise<void> {
  try { await ensureSession() } catch (cause) { error.value = safeError(cause, '暂时无法恢复登录，请稍后重试'); loadingTenants.value = false; return }
  if (!authState.authenticated) { loadingTenants.value = false; return }
  loadingTenants.value = true
  error.value = null
  const identity = tenantSwitchGuard.begin('load')
  try {
    const result = await myTenants(apiClient)
    if (!mounted || !tenantSwitchGuard.isCurrent(identity, 'load')) return
    tenants.value = result.items
    const preferred = typeof route.query.tenant === 'string' ? route.query.tenant : session.readActiveTenant()?.tenant_id
    const selected = availableTenants.value.find(item => item.tenant_id === preferred)
      ?? (!preferred && availableTenants.value.length === 1 ? availableTenants.value[0] : undefined)
    if (selected) await chooseTenant(selected)
    else if (!preferred) choosingTenant.value = true
    if (route.query.conversation || route.query.review) await restoreHistory()
  } catch (cause) {
    if (mounted && tenantSwitchGuard.isCurrent(identity, 'load')) error.value = safeError(cause, '无法读取租户列表')
  } finally {
    loadingTenants.value = false
  }
}

async function chooseTenant(tenant: AccountTenant): Promise<void> {
  if (switching.value || busy.value) return
  activeTenant.value = null
  switching.value = true
  const identity = tenantSwitchGuard.begin(tenant.tenant_id)
  try {
    await selectTenant({
      tenant_id: tenant.tenant_id,
      membership_id: tenant.membership_id,
      name: tenant.name,
    })
    if (!mounted || !tenantSwitchGuard.isCurrent(identity, tenant.tenant_id)) return
    activeTenant.value = tenant
    choosingTenant.value = false
  } catch (cause) {
    if (mounted && tenantSwitchGuard.isCurrent(identity, tenant.tenant_id)) {
      error.value = safeError(cause, '切换租户失败')
    }
  } finally {
    if (mounted && tenantSwitchGuard.isCurrent(identity, tenant.tenant_id)) {
      switching.value = false
    }
  }
  if (activeTenant.value?.tenant_id === tenant.tenant_id && !switching.value) {
    if (pendingFile.value) void uploadFile(pendingFile.value)
    else if (pendingSend.value) { pendingSend.value = false; void send() }
    else if (pendingDemo.value) { pendingDemo.value = false; void runDemo() }
  }
}

function startDemo(): void {
  if (busy.value || switching.value) return
  if (!authState.authenticated) { pendingDemo.value = true; showLogin.value = true; return }
  if (!activeTenant.value) {
    pendingDemo.value = true
    choosingTenant.value = true
    if (!loadingTenants.value && availableTenants.value.length === 1) void chooseTenant(availableTenants.value[0]!)
    return
  }
  void runDemo()
}

function startInterviewUpload(): void {
  if (busy.value || switching.value) return
  fileInput.value?.click()
}

async function runDemo(): Promise<void> {
  if (!activeTenant.value || busy.value) return
  clearPrivateState()
  pendingDemo.value = false
  fileName.value = '演示合同-房屋租赁.pdf'
  submittedInstruction.value = '演示：展示 Agent、MCP、RAG、证据和网页批注链路。'
  showPreview.value = true
  busy.value = true
  error.value = null
  const controller = new AbortController()
  activeRequest = controller
  const tenantId = activeTenant.value.tenant_id
  const requestIdentity = requestGuard.begin(tenantId)
  const api = createContractReviewApi({
    baseUrl: (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1',
    tokenProvider: () => session.readTenantToken(),
  })
  try {
    const result = await runDemoReview(api, tenantId, (streamEvent) => {
      if (requestGuard.isCurrent(requestIdentity, activeTenant.value?.tenant_id ?? null)
        && streamEvent.type === 'progress' && !stages.value.includes(streamEvent.stage)) {
        stages.value = [...stages.value, streamEvent.stage]
      }
    }, controller.signal)
    if (activeRequest !== controller || !requestGuard.isCurrent(requestIdentity, activeTenant.value?.tenant_id ?? null)) return
    run.value = result.run
    pdfBytes.value = result.pdf
  } catch (cause) {
    if (requestGuard.isCurrent(requestIdentity, activeTenant.value?.tenant_id ?? null)
      && !(cause instanceof DOMException && cause.name === 'AbortError')) {
      error.value = safeError(cause, '演示样例未完成')
    }
  } finally {
    if (activeRequest === controller) { activeRequest = null; busy.value = false }
  }
}

function leaveTenant(): void {
  tenantSwitchGuard.invalidate()
  switching.value = false
  clearPrivateState()
  session.clearTenantToken()
  session.clearActiveTenant()
  activeTenant.value = null
  choosingTenant.value = true
  refreshAuth()
}

async function upload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  acceptFiles(Array.from(input.files ?? []))
  input.value = ''
}

function acceptFiles(files: File[]): void {
  dragging.value = false
  dragDepth = 0
  if (!files.length) return
  if (busy.value) { error.value = new ApiError({ status: 409, code: 'busy', title: '请先停止当前任务，再上传新的合同。' }); return }
  if (files.length !== 1) { error.value = new ApiError({ status: 422, code: 'one_file', title: '一次只能审阅一份 PDF 合同。' }); return }
  const file = files[0]!
  if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
    error.value = new ApiError({ status: 422, code: 'pdf_required', title: '当前支持 PDF，请将 Word 导出为 PDF 后上传。' }); return
  }
  if (!file.size || file.size > MAX_PDF_BYTES) {
    error.value = new ApiError({ status: 422, code: 'file_too_large', title: '请上传非空且不超过 10 MB 的 PDF。' }); return
  }
  error.value = null
  pendingFile.value = file
  if (!authState.authenticated) { showLogin.value = true; return }
  if (!activeTenant.value) {
    choosingTenant.value = true
    if (!loadingTenants.value) {
      if (availableTenants.value.length === 1) void chooseTenant(availableTenants.value[0]!)
      else if (!tenants.value.length) void loadTenants()
    }
    return
  }
  void uploadFile(file)
}

function drop(event: DragEvent): void { acceptFiles(Array.from(event.dataTransfer?.files ?? [])) }
function dragEnter(event: DragEvent): void {
  if (event.dataTransfer?.types.includes('Files')) { dragDepth += 1; dragging.value = true }
}
function dragLeave(): void { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) dragging.value = false }
function preventFileNavigation(event: DragEvent): void {
  if (event.dataTransfer?.types.includes('Files')) event.preventDefault()
}
function dropOutsideWorkspace(event: DragEvent): void {
  if (!event.dataTransfer?.files.length) return
  event.preventDefault()
  drop(event)
}
function trackScroll(): void {
  const element = transcriptElement.value
  if (element) followTail.value = element.scrollHeight - element.scrollTop - element.clientHeight < 100
}

async function uploadFile(file: File): Promise<void> {
  if (!activeTenant.value || busy.value) return
  const requestedInstruction = instruction.value
  const requestedAsOf = asOf.value
  clearPrivateState()
  fileName.value = file.name
  submittedInstruction.value = requestedInstruction
  showPreview.value = true
  if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
    error.value = new ApiError({ status: 422, code: 'pdf_required', title: '第一版仅支持 PDF 合同' })
    return
  }
  if (file.size > MAX_PDF_BYTES) {
    error.value = new ApiError({ status: 422, code: 'file_too_large', title: 'PDF 超过 10MB 限制' })
    return
  }
  busy.value = true
  const controller = new AbortController()
  activeRequest = controller
  const tenantId = activeTenant.value.tenant_id
  const requestIdentity = requestGuard.begin(tenantId)
  const api = createContractReviewApi({
    baseUrl: (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api/v1',
    tokenProvider: () => session.readTenantToken(),
  })
  try {
    const localPreview = await readPdfPreview(file)
    if (!requestGuard.isCurrent(requestIdentity, activeTenant.value?.tenant_id ?? null)) return
    pdfBytes.value = localPreview
    const result = await runContractReview(
      api,
      {
        tenantId,
        file,
        instruction: requestedInstruction,
        asOf: requestedAsOf || undefined,
      },
      (streamEvent) => {
        if (
          requestGuard.isCurrent(requestIdentity, activeTenant.value?.tenant_id ?? null)
          && streamEvent.type === 'progress'
          && !stages.value.includes(streamEvent.stage)
        ) {
          stages.value = [...stages.value, streamEvent.stage]
        }
      },
      controller.signal,
    )
    if (
      activeRequest !== controller
      || !requestGuard.isCurrent(requestIdentity, activeTenant.value?.tenant_id ?? null)
    ) return
    run.value = result.run
    pdfBytes.value = result.pdf
    loadedHistory = `${tenantId}//${result.run.id}`
    void router.replace({ path: '/chat', query: { tenant: tenantId, review: result.run.id } })
  } catch (cause) {
    if (
      requestGuard.isCurrent(requestIdentity, activeTenant.value?.tenant_id ?? null)
      && !(cause instanceof DOMException && cause.name === 'AbortError')
    ) {
      error.value = safeError(cause, '合同审查失败')
    }
  } finally {
    notifyHistoryChanged()
    if (activeRequest === controller) {
      activeRequest = null
      busy.value = false
    }
  }
}

function stop(): void {
  for (const turn of turns.value) if (turn.status === 'streaming') turn.status = 'incomplete'
  requestGuard.invalidate()
  chatGuard.invalidate()
  activeRequest?.abort()
  activeRequest = null
  busy.value = false
  error.value = new ApiError({ status: 0, code: 'cancelled', title: '已停止生成，本次内容未完成。' })
}

async function send(): Promise<void> {
  if (busy.value || switching.value) return
  const content = instruction.value.trim()
  if (!authState.authenticated) { pendingSend.value = true; showLogin.value = true; return }
  if (pendingFile.value) { if (activeTenant.value) await uploadFile(pendingFile.value); else choosingTenant.value = true; return }
  if (!content) return
  if (!activeTenant.value) { pendingSend.value = true; choosingTenant.value = true; return }
  instruction.value = ''
  error.value = null
  busy.value = true
  turns.value.push({ role: 'user', content })
  turns.value.push({ role: 'assistant', content: '', status: 'streaming' })
  const index = turns.value.length - 1
  const controller = new AbortController()
  activeRequest = controller
  const tenantId = activeTenant.value.tenant_id
  const identity = chatGuard.begin(tenantId)
  const current = () => mounted && activeRequest === controller && chatGuard.isCurrent(identity, activeTenant.value?.tenant_id ?? null)
  let complete = false
  try {
    if (!conversationId.value) {
      const reviewId = run.value?.id
      const created = reviewId
        ? await createConversation(tenantId, content, reviewId)
        : await createConversation(tenantId, content)
      if (!current()) return
      conversationId.value = created.id
      loadedHistory = `${tenantId}/${created.id}/${reviewId ?? ''}`
      void router.replace({
        path: '/chat',
        query: {
          tenant: tenantId,
          conversation: created.id,
          ...(reviewId ? { review: reviewId } : {}),
        },
      })
      notifyHistoryChanged()
    }
    const stream = await sendConversationMessage(tenantId, conversationId.value, content, controller.signal)
    for await (const frame of stream) {
      if (!current()) return
      if (frame.event === 'delta' && typeof frame.data.text === 'string') turns.value[index]!.content += frame.data.text
      if (frame.event === 'error') throw new ApiError({ status: Number(frame.data.status ?? 0), code: String(frame.data.code ?? 'chat_failed'), title: '回答未完整生成，请重试。' })
      if (frame.event === 'done' && frame.data.status === 'completed' && turns.value[index]!.content.trim()) {
        complete = true
        turns.value[index]!.status = 'completed'
        break
      }
    }
    if (!complete && current()) throw new ApiError({ status: 0, code: 'incomplete', title: '回答未完整生成，请重试。' })
  } catch (cause) { if (current() && !controller.signal.aborted) error.value = safeError(cause, '对话未完成，请重试') }
  finally {
    notifyHistoryChanged()
    if (activeRequest === controller) {
      if (!complete) turns.value[index]!.status = 'incomplete'
      activeRequest = null
      busy.value = false
    }
  }
}

async function loggedIn(): Promise<void> {
  showLogin.value = false
  await loadTenants()
  if (pendingSend.value) { pendingSend.value = false; await send() }
}

watch(() => authState.authenticated, (authenticated) => {
  if (!authenticated) { leaveTenant(); tenants.value = []; choosingTenant.value = false }
})

watch(() => authState.accountIdentity, (identity, previous) => {
  if (previous && identity && previous !== identity) { leaveTenant(); tenants.value = []; void loadTenants() }
})
watch(() => authState.tenantIdentity, (identity, previous) => {
  if (previous && identity !== previous && !switching.value && activeTenant.value
    && session.readActiveTenant()?.tenant_id !== activeTenant.value.tenant_id) {
    clearPrivateState()
    activeTenant.value = null
    if (authState.authenticated) void loadTenants()
  }
})
watch(() => [route.query.tenant, route.query.conversation, route.query.review], () => { if (!loadingTenants.value) void restoreHistory() })

watch(() => [turns.value.at(-1)?.content, stages.value.length, run.value?.status], () => {
  const element = transcriptElement.value
  if (element && followTail.value) element.scrollTop = element.scrollHeight
}, { flush: 'post' })

onMounted(() => {
  window.addEventListener('dragover', preventFileNavigation)
  window.addEventListener('drop', dropOutsideWorkspace)
  void loadTenants()
})

onBeforeUnmount(() => {
  window.removeEventListener('dragover', preventFileNavigation)
  window.removeEventListener('drop', dropOutsideWorkspace)
  mounted = false
  tenantSwitchGuard.invalidate()
  clearPrivateState()
})
</script>

<template>
  <section class="workspace" :class="{ 'with-preview': pdfBytes && showPreview }" @dragenter.prevent="dragEnter" @dragover.prevent @dragleave.prevent="dragLeave" @drop.prevent.stop="drop">
    <header class="workspace-head">
      <div class="workspace-title">律师 Agent <span class="quiet">/</span> <span class="workspace-subtitle">{{ fileName ? '合同审阅' : '对话' }}</span></div>
      <div class="head-actions">
        <button v-if="authState.authenticated" class="subtle workspace-picker" :disabled="busy || switching" @click="leaveTenant">{{ activeTenant?.name || '选择工作空间' }} <span>⌄</span></button>
        <button v-if="pdfBytes" class="subtle" @click="showPreview = !showPreview">{{ showPreview ? '收起原文' : '查看原文' }}</button>
        <button v-if="hasConversation" class="subtle" :disabled="switching" @click="newConversation">新对话</button>
      </div>
    </header>
    <div class="conversation-pane">
      <div ref="transcriptElement" class="transcript" :class="{ welcome: !hasConversation }" @scroll="trackScroll">
        <div v-if="!hasConversation" class="welcome-content">
          <div class="agent-symbol" aria-hidden="true">✳</div>
          <p class="welcome-kicker">你的法律工作助手</p>
          <h1>今天，有什么可以帮你？</h1>
          <p class="welcome-copy">面试官给你一份合同，Agent 会现场完成审阅、检索、引用和批注。</p>
          <div class="interview-card">
            <span class="interview-label">面试演示 · 真实合同</span>
            <h2>上传合同，开始完整审阅</h2>
            <p>选择 PDF，或直接把合同拖到当前页面。上传后自动进入 Qwen、多轮 MCP、第二版 RAG 和 PDF 批注流程。</p>
            <button class="interview-upload" type="button" @click="startInterviewUpload">选择面试官的合同</button>
            <small>支持 PDF · 最大 10 MB · 最多 30 页</small>
          </div>
          <div class="suggestions">
            <button class="demo-suggestion" @click="startDemo"><span>▶</span><strong>查看固定样例</strong><small>无模型额度时展示完整界面链路</small></button>
            <button @click="instruction = '帮我整理签订租赁合同前需要确认的问题。'"><span>◎</span><strong>梳理关注事项</strong><small>把复杂的问题拆解清楚</small></button>
          </div>
        </div>
        <div v-else class="message-list" aria-live="polite">
          <template v-if="fileName">
            <article class="message user"><div class="message-body"><div class="attachment"><span class="pdf-icon">PDF</span><div><strong>{{ fileName }}</strong><small>合同附件</small></div></div><p>{{ submittedInstruction || '帮我审阅一下这份合同。' }}</p></div></article>
            <article class="message assistant"><span class="avatar" aria-hidden="true">✳</span><div class="review-response">
              <p v-if="busy" class="working" role="status"><span class="pulse" />{{ latestStage }}</p>
              <details v-if="stages.length" class="stage-details"><summary>{{ busy ? '查看审阅进度' : '查看处理步骤' }}</summary><ol><li v-for="stage in stages" :key="stage">{{ STAGE_LABELS[stage] }}</li></ol></details>
              <aside v-if="verificationWarnings.length || stages.includes('content_warning')" class="reading-warning" role="note"><strong>内容核对提醒 · 不影响审阅继续</strong><p>结果基于当前可读内容，请对照原件核对以下位置。</p><ul v-if="verificationWarnings.length"><li v-for="warning in verificationWarnings" :key="warning">{{ warning }}</li></ul></aside>
              <template v-if="run?.status === 'draft'"><div v-if="run.is_demo" class="demo-badge">演示模式 · 固定样例，不消耗模型额度</div><h2>审阅完成，生成 {{ run.issues.length }} 条风险草稿</h2><p class="muted">{{ run.message || '引用匹配不等于法律结论成立，请由律师或法务复核。' }}</p><p v-if="run.withheld_issues?.length" class="verification-warning">{{ run.withheld_issues.length }} 条候选意见因引用未通过核验，未发布为批注。涉及第 {{ [...new Set(run.withheld_issues.map(item => item.page))].join('、') }} 页，需人工核对；不代表合同没有其他风险。</p><div class="result-actions"><button class="document-link" @click="showPreview = true">▤ 查看原文与批注 ↗</button><button v-if="run.mcp_tools?.length" class="document-link" @click="showToolCatalog = !showToolCatalog">⚙ {{ showToolCatalog ? '收起' : '查看' }} MCP 工具链</button></div><section v-if="showToolCatalog" class="tool-catalog" aria-label="动态发现的 MCP 工具"><header><strong>MCP 工具目录</strong><small>由 Server 动态发现，网页只展示安全摘要</small></header><div v-for="tool in run.mcp_tools" :key="tool.name" class="tool-row"><span class="tool-status">{{ tool.status === 'authorized' ? '已授权' : '已发现' }}</span><div><strong>{{ tool.name }}</strong><small>{{ tool.capability }} · {{ tool.description }}</small></div></div></section><div v-for="(issue, index) in run.issues" :key="index" class="finding"><span class="finding-number">{{ index + 1 }}</span><div><small v-if="issue.anchor_id === null" class="muted">位置待核对 · 基于模型转录，未精确标红</small><small v-else-if="issue.highlights_complete === false" class="muted">已标出原文中可确认的位置</small><h3>{{ issue.problem }}</h3><blockquote>{{ issue.quote }}</blockquote><p>{{ issue.suggestion }}</p><section v-if="issue.evidence_passages?.length" class="evidence-details" aria-label="引用的法律原文"><h4>法律原文（检索片段）</h4><blockquote v-for="citation in issue.evidence_passages" :key="citation.passage_id"><strong>《{{ run.evidence_documents?.find(doc => doc.document_id === citation.document_id)?.title || '法律名称待核对' }}》</strong><p>{{ citation.quote }}</p></blockquote><small class="muted">引用匹配不等于法律结论成立，适用性仍需核验。</small></section><p v-else class="muted">{{ issue.evidence_ids.length ? '该记录仅保留引用编号，未保存条文原文。' : '本条未附法律原文引用。' }}</p></div></div></template>
              <p v-else-if="run?.status === 'no_evidence'">{{ run.message || '我没有找到相关的法律文档，暂时无法提供相关服务' }}</p>
              <p v-else-if="!busy && !error">审阅尚未完成。</p>
            </div></article>
          </template>
          <button v-if="earlierCursor" class="subtle" :disabled="loadingEarlier" @click="loadEarlierMessages">{{ loadingEarlier ? '正在加载…' : '查看更早消息' }}</button>
          <article v-for="(turn, index) in turns" :key="index" class="message" :class="turn.role">
            <span v-if="turn.role === 'assistant'" class="avatar" aria-hidden="true">✳</span>
            <div class="message-body">{{ turn.content || (turn.status === 'streaming' ? '正在生成…' : '本次回答未完成。') }}<small v-if="turn.status === 'incomplete'" class="incomplete-label">回答未完成 · 不作为后续对话依据</small></div>
          </article>
        </div>
      </div>
      <div class="composer-area">
        <div v-if="choosingTenant" class="workspace-options"><strong>选择会话所属工作空间</strong><span v-if="loadingTenants">正在加载…</span><button v-for="tenant in availableTenants" :key="tenant.tenant_id" :disabled="switching" @click="chooseTenant(tenant)">{{ tenant.name }}</button><p v-if="!loadingTenants && !availableTenants.length">当前账号没有可用工作空间，请先创建或加入租户。</p></div>
        <ErrorNote :error="error" />
        <p v-if="error?.code === 'model_provider_unavailable'" class="model-error">对话模型暂不可用，请检查服务端模型配置后重试。</p>
        <button v-if="error?.code === 'authentication_failed'" class="subtle" @click="showLogin = true">重新登录</button>
        <form class="composer" @submit.prevent="send">
          <div v-if="pendingFile" class="pending-file"><span class="pdf-icon">PDF</span>{{ pendingFile.name }}<button type="button" class="subtle" aria-label="移除附件" @click="pendingFile = null">×</button></div>
          <textarea v-model="instruction" :maxlength="MAX_CONTENT_CHARS" :disabled="busy" rows="2" aria-label="消息或审阅要求" placeholder="发送消息，或拖入 PDF 合同…" @keydown.enter.exact="(event) => { if (!event.isComposing) { event.preventDefault(); void send() } }" />
          <div class="composer-tools"><div class="composer-left"><button type="button" class="attach-button" :disabled="busy || switching" aria-label="添加 PDF 合同" title="添加 PDF 合同" @click="fileInput?.click()">＋</button><details class="review-options"><summary>PDF 审阅选项</summary><label>目标日期（可选）<input v-model="asOf" type="date" :disabled="busy" /></label></details></div><button v-if="busy" type="button" class="send-button" aria-label="停止生成" title="停止生成" @click="stop">■</button><button v-else type="submit" class="send-button" :disabled="switching || (!instruction.trim() && !pendingFile)" aria-label="发送消息" title="发送">↑</button></div>
          <input :key="inputKey" ref="fileInput" class="file-input" type="file" accept="application/pdf,.pdf" @change="upload" />
        </form>
        <p class="composer-footnote">{{ fileName ? '审阅意见为草稿，需人工复核。' : 'AI 回答可能有误，重要法律信息请核验。' }} <span>PDF ≤ 10 MB · 30 页</span></p>
      </div>
    </div>
    <aside v-if="pdfBytes && showPreview" class="document-pane" aria-label="PDF 原文预览"><header><div><span class="pdf-icon">PDF</span><strong>{{ fileName }}</strong></div><button class="subtle" aria-label="关闭原文预览" @click="showPreview = false">×</button></header><div class="document-scroll"><PdfContractReview :run="run" :pdf-bytes="pdfBytes" /></div></aside>
    <div v-if="dragging" class="drop-overlay"><div class="drop-card"><span>↓</span><h2>将合同放在这里</h2><p>松开后开始审阅 · PDF，最大 10 MB</p></div></div>
    <AuthModal v-if="showLogin" @close="showLogin = false" @success="loggedIn" />
  </section>
</template>

<style scoped>
.reading-warning { margin: 1rem 0; padding: .85rem 1rem; border: 1px solid #e8ce91; border-radius: 12px; background: #fffbef; color: #735318; font-size: .9rem; }
.reading-warning p { margin: .4rem 0; }
.workspace { height: 100dvh; display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: 64px minmax(0, 1fr); position: relative; background: #fff; color: #242424; }
.workspace.with-preview { grid-template-columns: minmax(360px, .9fr) minmax(380px, 1.1fr); }
.workspace-head { grid-column: 1 / -1; display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 0 28px; }
.workspace-title { font-weight: 650; font-size: 17px; white-space: nowrap; }.workspace-subtitle { font-weight: 400; color: #727272; font-size: 14px; }.quiet { color: #ddd; padding: 0 12px; }
.head-actions { display: flex; gap: 6px; align-items: center; }.subtle { background: transparent; color: #666; border: 0; padding: 7px 10px; border-radius: 8px; font-size: 13px; }.subtle:hover { background: #f0f0f0; }.workspace-picker { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.workspace-picker span { margin-left: 12px; }
.conversation-pane { min-width: 0; min-height: 0; display: flex; flex-direction: column; }.transcript { flex: 1; min-height: 0; overflow-y: auto; padding: 24px 32px; }.transcript.welcome { display: flex; align-items: center; justify-content: center; }
.welcome-content { width: min(100%, 670px); padding: 10px 0 30px; text-align: center; }.agent-symbol { font-size: 48px; line-height: 1; color: #2d5045; margin-bottom: 22px; }.welcome-kicker { font-size: 12px; letter-spacing: 3px; color: #8b8b87; margin: 0 0 12px; }.welcome-content h1 { font-weight: 550; letter-spacing: -1px; font-size: clamp(26px, 3vw, 36px); margin: 0 0 14px; }.welcome-copy { color: #666; font-size: 14px; margin: 0; }.interview-card { margin: 28px auto 0; padding: 24px 26px 20px; width: min(100%, 560px); box-sizing: border-box; border: 1px solid #c8dbcd; border-radius: 20px; background: linear-gradient(135deg, #f7fbf7, #f1f7f2); text-align: left; box-shadow: 0 10px 28px #365f4210; }.interview-label { display: inline-flex; padding: 4px 8px; border-radius: 999px; background: #e3f0e5; color: #376149; font-size: 11px; font-weight: 650; letter-spacing: .3px; }.interview-card h2 { margin: 12px 0 7px; font-size: 19px; font-weight: 600; }.interview-card p { margin: 0 0 16px; color: #637067; font-size: 13px; line-height: 1.65; }.interview-upload { padding: 10px 16px; border: 0; border-radius: 10px; background: #2f5a46; color: white; font-size: 13px; cursor: pointer; }.interview-upload:hover { background: #234936; }.interview-card small { margin-left: 10px; color: #89938b; font-size: 11px; }
.suggestions { display: flex; justify-content: center; gap: 12px; margin-top: 34px; }.suggestions button { width: 225px; padding: 18px; text-align: left; background: white; border: 1px solid #e7e7e4; border-radius: 15px; color: #333; transition: background .15s; }.suggestions button:hover { background: #fafaf8; border-color: #ccc; }.suggestions span { display: block; font-size: 21px; color: #58685e; margin-bottom: 12px; }.suggestions strong { display: block; font-size: 14px; font-weight: 500; }.suggestions small { display: block; font-size: 12px; margin-top: 6px; color: #888; }.demo-suggestion { border-color: #b6c9bb !important; background: #f7fbf7 !important; }.demo-badge { display: inline-flex; padding: 5px 9px; border-radius: 999px; background: #edf6ef; color: #3c684d; font-size: 12px; margin-bottom: 10px; }.result-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }.tool-catalog { border: 1px solid #dce7df; border-radius: 13px; background: #f8fbf8; padding: 12px; margin: 12px 0 18px; }.tool-catalog header { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; margin-bottom: 8px; }.tool-catalog header small, .tool-row small { display: block; color: #7b847d; font-size: 11px; margin-top: 3px; }.tool-row { display: flex; gap: 8px; padding: 9px 0; border-top: 1px solid #e4eee6; align-items: flex-start; }.tool-row strong { font-size: 12px; font-weight: 600; }.tool-status { flex: 0 0 auto; padding: 3px 6px; border-radius: 5px; background: #e4f1e7; color: #3c684d; font-size: 10px; }
.message-list { max-width: 760px; margin: 0 auto; display: flex; flex-direction: column; gap: 30px; padding: 10px 0 30px; }.message { display: flex; align-items: flex-start; gap: 15px; font-size: 15px; line-height: 1.85; }.message.user { align-self: flex-end; max-width: 88%; }.message.user .message-body { background: #f2f2f2; padding: 12px 20px; border-radius: 22px; }.message-body { white-space: pre-wrap; overflow-wrap: anywhere; }.avatar { color: #37594b; font-size: 26px; line-height: 1.4; }.review-response { min-width: 0; flex: 1; }.review-response h2 { font-size: 18px; font-weight: 600; }.muted { color: #858585; font-size: 13px; }.attachment { display: flex; gap: 12px; align-items: center; }.attachment strong { display: block; font-size: 13px; overflow-wrap: anywhere; }.attachment small { display: block; color: #888; font-size: 11px; }.pdf-icon { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 38px; flex-shrink: 0; border-radius: 7px; color: #b8574c; background: #faebe8; font-size: 10px; font-weight: 700; }
.working { display: flex; gap: 10px; align-items: center; margin-top: 0; color: #555; }.pulse { width: 8px; height: 8px; border-radius: 50%; background: #53745f; animation: pulse 1.4s infinite; }@keyframes pulse { 50% { opacity: .25; } }.stage-details { color: #888; font-size: 12px; }.stage-details summary { cursor: pointer; }.stage-details ol { line-height: 2; padding-left: 20px; }.document-link { padding: 10px 16px; background: #fff; border: 1px solid #dededb; border-radius: 10px; color: #3c5848; font-size: 13px; }.finding { display: flex; gap: 12px; margin-top: 24px; }.finding-number { background: #f5f3ef; color: #806d53; border-radius: 50%; width: 24px; height: 24px; text-align: center; line-height: 24px; flex-shrink: 0; font-size: 12px; }.finding h3 { margin: 0; font-size: 14px; }.finding blockquote { margin: 10px 0; padding-left: 12px; border-left: 2px solid #e8ceca; color: #8a7773; font-size: 13px; }.finding p { font-size: 14px; }
.composer-area { width: min(100%, 824px); margin: 0 auto; padding: 12px 32px 16px; }.composer { position: relative; border: 1px solid #e4e4e4; box-shadow: 0 3px 16px #00000005; border-radius: 24px; background: #fff; padding: 16px 16px 11px; }.composer:focus-within { border-color: #bfc9c3; }.composer textarea { width: 100%; display: block; padding: 0 6px; border: 0; outline: 0; background: transparent; resize: none; font-size: 15px; line-height: 1.65; min-height: 55px; }.composer textarea::placeholder { color: #999; }.composer-tools, .composer-left { display: flex; align-items: center; }.composer-tools { justify-content: space-between; }.composer-left { gap: 9px; color: #929292; font-size: 12px; }.attach-button { border: 0; background: transparent; color: #555; font-size: 26px; width: 34px; height: 34px; padding: 0; border-radius: 50%; }.attach-button:hover { background: #eee; }.send-button { width: 34px; height: 34px; border: 0; border-radius: 50%; background: #292929; color: white; font-size: 23px; line-height: 30px; padding: 0; }.send-button:disabled { background: #e7e7e7; color: #aaa; opacity: 1; }.file-input { display: none; }.composer-footnote { font-size: 11px; color: #a0a0a0; text-align: center; margin: 10px 0 0; }.composer-footnote span { margin-left: 8px; }.pending-file { display: flex; align-items: center; gap: 10px; font-size: 12px; margin-bottom: 10px; }.workspace-options { font-size: 13px; padding: 12px; border: 1px solid #e4e4e4; border-radius: 12px; margin-bottom: 10px; }.workspace-options strong { display: block; margin-bottom: 8px; }.workspace-options button { background: #f4f4f2; color: #444; border: 1px solid #ddd; margin: 4px; }.model-error { color: #945a4a; font-size: 13px; }
.document-pane { display: flex; flex-direction: column; min-height: 0; min-width: 0; border-left: 1px solid #e8e8e8; background: #f5f5f3; }.document-pane > header { padding: 13px 20px; display: flex; align-items: center; justify-content: space-between; gap: 8px; border-bottom: 1px solid #e6e6e6; background: white; }.document-pane > header div { display: flex; align-items: center; gap: 10px; min-width: 0; }.document-pane strong { font-size: 12px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.document-scroll { flex: 1; overflow: auto; min-height: 0; padding: 24px; }.document-scroll :deep(.issue-popover) { width: min(100%, 34rem); box-sizing: border-box; }.document-scroll :deep(.page-nav) { top: 0; flex-wrap: wrap; }
.drop-overlay { position: absolute; inset: 0; z-index: 30; display: grid; place-items: center; background: #ffffffed; pointer-events: none; border: 2px dashed #789585; border-radius: 12px; }.drop-card { text-align: center; }.drop-card > span { font-size: 42px; color: #53775f; }.drop-card h2 { font-size: 24px; font-weight: 500; }.drop-card p { color: #888; font-size: 13px; }
@media (prefers-reduced-motion: reduce) { .pulse { animation: none; } }
@media (max-width: 1050px) { .workspace.with-preview { grid-template-columns: minmax(300px, 1fr) minmax(320px, 1fr); }.transcript { padding: 20px; }.composer-area { padding: 10px 18px 14px; }.suggestions { flex-wrap: wrap; }.suggestions button { width: 100%; }.document-scroll { padding: 12px; } }
@media (max-width: 760px) { .workspace { height: calc(100dvh - 52px); }.workspace.with-preview { grid-template-columns: minmax(0, 1fr); }.workspace-head { padding: 0 15px; }.workspace-title { font-size: 15px; }.workspace-subtitle, .quiet { display: none; }.workspace-picker { max-width: 110px; font-size: 11px; }.document-pane { position: absolute; inset: 64px 0 0; z-index: 15; border-left: 0; }.welcome-content h1 { font-size: 27px; }.interview-card { padding: 20px; }.interview-card small { display: block; margin: 9px 0 0; }.suggestions { margin-top: 25px; }.suggestions button { width: min(100%, 290px); padding: 14px; }.suggestions span { display: none; }.welcome-content { padding: 0; }.composer-footnote span { display: none; }.transcript { padding: 12px 20px; }.message.user { max-width: 94%; } }
.review-options { position: relative; }.review-options summary { cursor: pointer; list-style: none; }.review-options label { position: absolute; bottom: 30px; left: 0; z-index: 25; display: grid; gap: 8px; width: 210px; padding: 15px; border: 1px solid #e2e2df; border-radius: 12px; box-shadow: 0 6px 24px #0001; background: #fff; color: #777; }.review-options input { font-size: 13px; }
@media (min-width: 761px) and (max-width: 980px) { .workspace.with-preview { grid-template-columns: minmax(0, 1fr); }.document-pane { position: absolute; inset: 64px 0 0; z-index: 15; } }
.incomplete-label { display: block; color: #9b795f; font-size: 12px; margin-top: 8px; }
</style>
