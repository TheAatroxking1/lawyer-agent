<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiError, apiClient, myTenants, switchTenant } from '../api'
import type {
  AccountTenant,
  DocumentHeaderSummary,
  DocumentSummary,
  MatterKind,
  MatterSummary,
} from '../api'
import { createMatter, downloadReport, listDocuments, listMatters, listRiskIssues, registerDocument } from '../api/tenant'
import type { RiskIssueSummary } from '../api'
import ErrorNote from '../components/ErrorNote.vue'
import { session } from '../auth/session'

const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

const MEMBERSHIP_LABELS: Record<string, string> = {
  active: '已生效',
  pending_verification: '待验证',
  suspended: '已停用',
  left: '已退出',
}
const TENANT_LABELS: Record<string, string> = {
  active: '可用',
  pending_verification: '待审核',
  suspended: '停用',
}
const MATTER_STATUS_LABELS: Record<string, string> = {
  open: '新建',
  active: '进行中',
  closed: '已结案',
  archived: '已归档',
}
const KIND_LABELS: Record<MatterKind, string> = {
  contract_review: '合同审查',
  litigation: '诉讼',
  legal_advice: '法律咨询',
  compliance: '合规',
  other: '其他',
}
const MATTER_KINDS: Array<{ value: MatterKind; label: string }> = (
  Object.keys(KIND_LABELS) as MatterKind[]
).map((value) => ({ value, label: KIND_LABELS[value] }))

function membershipLabel(value: string): string {
  return MEMBERSHIP_LABELS[value] ?? value
}

const tenants = ref<AccountTenant[]>([])
const loading = ref(false)
const error = ref<ApiError | null>(null)
const switchingId = ref<string | null>(null)
const activeTenant = ref<AccountTenant | null>(null)
const switchError = ref<ApiError | null>(null)

// matter + document workspace state
const matters = ref<MatterSummary[]>([])
const mattersLoading = ref(false)
const mattersError = ref<ApiError | null>(null)
const showCreate = ref(false)
const newTitle = ref('')
const newKind = ref<MatterKind>('contract_review')
const creating = ref(false)
const createError = ref<ApiError | null>(null)
const expandedMatterId = ref<string | null>(null)
const documents = ref<Record<string, DocumentHeaderSummary[]>>({})
const documentsLoading = ref<Record<string, boolean>>({})
const uploadingMatterId = ref<string | null>(null)
const uploadError = ref<ApiError | null>(null)
const lastUpload = ref<DocumentSummary | null>(null)
const issuesByDoc = ref<Record<string, RiskIssueSummary[]>>({})
const issuesLoading = ref<string | null>(null)
const reportDownloading = ref<string | null>(null)
const reportError = ref<ApiError | null>(null)

async function load(): Promise<void> {
  error.value = null
  loading.value = true
  try {
    const result = await myTenants(apiClient)
    tenants.value = result.items
  } catch (cause) {
    error.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    loading.value = false
  }
}

async function enterTenant(tenant: AccountTenant): Promise<void> {
  if (switchingId.value !== null) return
  switchError.value = null
  switchingId.value = tenant.tenant_id
  try {
    const result = await switchTenant(apiClient, {
      tenant_id: tenant.tenant_id,
      membership_id: tenant.membership_id,
    })
    session.saveTenantToken(result.access_token)
    activeTenant.value = tenant
    expandedMatterId.value = null
    documents.value = {}
    await refreshMatters()
  } catch (cause) {
    switchError.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    switchingId.value = null
  }
}

function leaveTenant(): void {
  session.clearTenantToken()
  activeTenant.value = null
  matters.value = []
  documents.value = {}
  showCreate.value = false
  switchError.value = null
}

async function refreshMatters(): Promise<void> {
  if (!activeTenant.value) return
  mattersError.value = null
  mattersLoading.value = true
  try {
    const page = await listMatters(activeTenant.value.tenant_id)
    matters.value = page.items
  } catch (cause) {
    mattersError.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    mattersLoading.value = false
  }
}

async function submitCreateMatter(): Promise<void> {
  if (!activeTenant.value || creating.value) return
  createError.value = null
  const title = newTitle.value.trim()
  if (title.length === 0) return
  creating.value = true
  try {
    await createMatter(activeTenant.value.tenant_id, { title, kind: newKind.value })
    newTitle.value = ''
    showCreate.value = false
    await refreshMatters()
  } catch (cause) {
    createError.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    creating.value = false
  }
}

async function toggleMatter(matterId: string): Promise<void> {
  if (expandedMatterId.value === matterId) {
    expandedMatterId.value = null
    return
  }
  expandedMatterId.value = matterId
  await loadDocuments(matterId)
}

async function loadDocuments(matterId: string): Promise<void> {
  if (!activeTenant.value) return
  documentsLoading.value[matterId] = true
  try {
    documents.value[matterId] = await listDocuments(
      activeTenant.value.tenant_id,
      matterId,
    )
  } finally {
    documentsLoading.value[matterId] = false
  }
}

async function inspectIssues(docId: string): Promise<void> {
  if (!activeTenant.value) return
  reportError.value = null
  issuesLoading.value = docId
  try {
    issuesByDoc.value[docId] = await listRiskIssues(activeTenant.value.tenant_id, docId)
  } catch (cause) {
    reportError.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法获取风险项，请稍后重试' })
  } finally {
    issuesLoading.value = null
  }
}

async function downloadDocReport(docId: string): Promise<void> {
  if (!activeTenant.value || reportDownloading.value !== null) return
  reportError.value = null
  reportDownloading.value = docId
  try {
    const { fileName, blob } = await downloadReport(activeTenant.value.tenant_id, docId)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = fileName
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (cause) {
    reportError.value = new ApiError({
      status: 0,
      code: 'report_unavailable',
      title: cause instanceof Error ? cause.message : '报告下载失败',
    })
  } finally {
    reportDownloading.value = null
  }
}

async function onUploadFile(matterId: string, event: Event): Promise<void> {
  if (!activeTenant.value || uploadingMatterId.value !== null) return
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploadError.value = null
  lastUpload.value = null
  if (file.size > 25 * 1024 * 1024) {
    uploadError.value = new ApiError({
      status: 422,
      code: 'file_too_large',
      title: '文件超过 25MB 限制',
    })
    input.value = ''
    return
  }
  uploadingMatterId.value = matterId
  try {
    const buffer = await file.arrayBuffer()
    const bytes = new Uint8Array(buffer)
    let binary = ''
    for (const byte of bytes) {
      binary += String.fromCharCode(byte)
    }
    const payloadB64 = btoa(binary)
    const summary = await registerDocument(activeTenant.value.tenant_id, matterId, {
      file_name: file.name,
      mime_type: file.type || DOCX_MIME,
      payload_b64: payloadB64,
    })
    lastUpload.value = summary
    await loadDocuments(matterId)
  } catch (cause) {
    uploadError.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '上传失败，请稍后重试' })
  } finally {
    uploadingMatterId.value = null
    input.value = ''
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <section class="workbench">
    <header class="head">
      <div>
        <h1>案件文档（工作台）</h1>
        <p class="lead">租户级案件、文档上传登记与复核。业务数据仅限本律所/团队可见。</p>
      </div>
      <button v-if="activeTenant" class="ghost" type="button" @click="leaveTenant">
        退出当前租户
      </button>
    </header>

    <ErrorNote :error="error" context="读取租户列表失败" />
    <p v-if="loading" class="state">加载中…</p>

    <section v-if="!loading && error === null && tenants.length === 0" class="guide">
      <h2>你还没有可用的律所/团队租户</h2>
      <p>案件与文档属于租户内部数据。加入流程（任选其一）：</p>
      <ol>
        <li>由某律所管理员向你发送<strong>邀请</strong>（按其邮箱/手机号投递并接受后成为成员）；</li>
        <li>或自行<strong>申请创建租户</strong>，经平台审核通过后获得 owner 成员资格；</li>
        <li>平台首个管理员由部署方以一次性 CLI（bootstrap_platform_admin）提权产生。</li>
      </ol>
      <p class="note">账号公开注册只创建身份，不自动归属任何租户——这是刻意的多租户边界。</p>
    </section>

    <section v-else-if="tenants.length > 0 && !activeTenant" class="tenant-list">
      <h2>选择租户进入</h2>
      <p v-if="switchError" class="error" role="alert">
        <span class="code">{{ switchError.code }}</span><span>{{ switchError.title }}</span>
      </p>
      <ul class="rows">
        <li v-for="tenant in tenants" :key="tenant.tenant_id" class="card">
          <div class="main">
            <strong>{{ tenant.name }}</strong>
            <span class="chip">{{ tenant.tenant_type }}</span>
          </div>
          <p class="meta">
            成员状态 <strong>{{ membershipLabel(tenant.membership_status) }}</strong>
          </p>
          <button
            v-if="tenant.membership_status === 'active' && tenant.tenant_status === 'active'"
            type="button"
            :disabled="switchingId !== null"
            @click="enterTenant(tenant)"
          >
            {{ switchingId === tenant.tenant_id ? '进入中…' : '进入工作台' }}
          </button>
          <p v-else class="pending">该成员/租户尚未生效（租户状态：{{ TENANT_LABELS[tenant.tenant_status] ?? tenant.tenant_status }}）。</p>
        </li>
      </ul>
    </section>

    <section v-if="activeTenant" class="active">
      <div class="active-head">
        <h2>当前租户：{{ activeTenant.name }}</h2>
        <button class="ghost" type="button" :disabled="mattersLoading" @click="refreshMatters">
          刷新案件
        </button>
      </div>

      <ErrorNote :error="mattersError" context="加载案件失败" />

      <div class="toolbar">
        <button type="button" @click="showCreate = !showCreate">
          {{ showCreate ? '收起' : '新建案件' }}
        </button>
        <button v-if="expandedMatterId" class="ghost" type="button" @click="expandedMatterId = null">
          收起文档区
        </button>
      </div>

      <form v-if="showCreate" class="create-form" @submit.prevent="submitCreateMatter">
        <ErrorNote :error="createError" context="创建失败" />
        <label>
          案件名称
          <input v-model="newTitle" required placeholder="如：A 公司房屋租赁合同纠纷" />
        </label>
        <label>
          类型
          <select v-model="newKind">
            <option v-for="kind in MATTER_KINDS" :key="kind.value" :value="kind.value">{{ kind.label }}</option>
          </select>
        </label>
        <button type="submit" :disabled="creating || newTitle.trim().length === 0">
          {{ creating ? '创建中…' : '创建案件' }}
        </button>
      </form>

      <p v-if="mattersLoading" class="state">案件加载中…</p>
      <p v-else-if="matters.length === 0 && !mattersError" class="state">暂无案件，先「新建案件」。</p>
      <ul v-else class="rows">
        <li v-for="matter in matters" :key="matter.id" class="card matter">
          <button class="matter-head" type="button" @click="toggleMatter(matter.id)">
            <div class="main">
              <strong>{{ matter.title }}</strong>
              <span class="chip">{{ KIND_LABELS[matter.kind as MatterKind] ?? matter.kind }}</span>
              <span class="chip">{{ MATTER_STATUS_LABELS[matter.status] ?? matter.status }}</span>
            </div>
            <span class="chevron">{{ expandedMatterId === matter.id ? '收起 ▲' : '文档 ▼' }}</span>
          </button>

          <div v-if="expandedMatterId === matter.id" class="documents">
            <div class="upload-row">
              <label class="file">
                <span>{{ uploadingMatterId === matter.id ? '上传中…' : '上传 DOCX 登记' }}</span>
                <input
                  type="file"
                  accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                  :disabled="uploadingMatterId !== null"
                  @change="onUploadFile(matter.id, $event)"
                />
              </label>
            </div>
            <p v-if="documentsLoading[matter.id]" class="state">文档加载中…</p>
            <p v-else-if="(documents[matter.id] ?? []).length === 0" class="state">
              该案件暂无文档。
            </p>
            <ul v-else class="doc-list">
              <li v-for="doc in documents[matter.id] ?? []" :key="doc.id">
                <div class="doc-line">
                  <span>{{ doc.display_name }}（v{{ doc.current_version_no }}）</span>
                  <span class="doc-actions">
                    <button type="button" :disabled="issuesLoading !== null" @click="inspectIssues(doc.id)">
                      {{ issuesLoading === doc.id ? '读取中…' : '风险项' }}
                    </button>
                    <button
                      type="button"
                      :disabled="reportDownloading !== null"
                      @click="downloadDocReport(doc.id)"
                    >
                      {{ reportDownloading === doc.id ? '下载中…' : '报告下载' }}
                    </button>
                  </span>
                </div>
                <ul v-if="(issuesByDoc[doc.id] ?? []).length > 0" class="issues">
                  <li v-for="issue in issuesByDoc[doc.id] ?? []" :key="issue.id">
                    <strong>{{ issue.risk_level }}</strong> {{ issue.matched_text }}（{{ issue.provision_no }} · {{ issue.status }}）
                  </li>
                </ul>
                <p v-else-if="issuesLoading !== doc.id && issuesByDoc[doc.id]" class="state">暂无风险项。</p>
              </li>
            </ul>
            <ErrorNote :error="reportError" context="风险项/报告操作失败" />
            <p v-if="lastUpload" class="ok">
              已登记：{{ lastUpload.file_name }} · v{{ lastUpload.version_no }} ·
              upload={{ lastUpload.upload_status }}{{ lastUpload.review_status ? ' · review=' + lastUpload.review_status : '' }}
            </p>
            <ErrorNote :error="uploadError" context="上传失败" />
          </div>
        </li>
      </ul>
    </section>
  </section>
</template>

<style scoped>
.workbench { display: grid; gap: 1.1rem; max-width: 54rem; }
.head, .active-head, .main { display: flex; align-items: center; gap: 0.6rem; }
.head, .active-head { justify-content: space-between; }
.head h1, .active-head h2 { margin: 0; }
.head h1 { font-size: 1.35rem; }
.lead, .meta, .note, .pending, .state { color: var(--color-text-muted); }
.lead, .note { font-size: 0.88rem; }
.head .lead { margin: 0.3rem 0 0; }
.active-head h2 { font-size: 1.05rem; }
.ghost { border: 1px solid var(--color-border); background: transparent; color: var(--color-text-secondary); }
.guide, .card { border: 1px solid var(--color-border); border-radius: 10px; padding: 0.9rem 1rem; background: var(--color-surface); }
.guide ol { display: grid; gap: 0.4rem; margin: 0.4rem 0; padding-left: 1.3rem; color: var(--color-text-secondary); line-height: 1.6; }
.rows { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.7rem; }
.card { display: grid; gap: 0.4rem; }
.chip { border: 1px solid var(--color-accent-soft-border); background: var(--color-accent-soft); color: var(--color-accent); border-radius: 999px; padding: 0.02rem 0.5rem; font-size: 0.78rem; }
.error { display: flex; gap: 0.5rem; align-items: baseline; margin: 0.4rem 0; padding: 0.5rem 0.7rem; border: 1px solid var(--color-danger-border); background: var(--color-danger-bg); color: var(--color-danger-text); border-radius: 6px; font-size: 0.88rem; }
.code { font-family: var(--font-mono); font-size: 0.78rem; }
.ok { margin: 0.3rem 0 0; color: #1e7d4a; font-size: 0.85rem; }
.toolbar { display: flex; gap: 0.6rem; }
.create-form { display: flex; flex-wrap: wrap; gap: 0.8rem; align-items: flex-end; padding: 0.9rem 1rem; border: 1px dashed var(--color-border); border-radius: 10px; }
.create-form label { display: grid; gap: 0.25rem; font-size: 0.85rem; color: var(--color-text-secondary); }
.matter { padding: 0; }
.matter-head { display: flex; justify-content: space-between; align-items: center; width: 100%; border: none; background: transparent; color: inherit; cursor: pointer; padding: 0.85rem 1rem; text-align: left; }
.chevron { color: var(--color-text-muted); font-size: 0.8rem; white-space: nowrap; }
.documents { border-top: 1px solid var(--color-border); padding: 0.8rem 1rem; display: grid; gap: 0.5rem; }
.upload-row .file { display: inline-block; }
.file input { max-width: 16rem; }
.doc-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.3rem; font-size: 0.9rem; color: var(--color-text-secondary); }
.doc-line { display: flex; justify-content: space-between; align-items: center; gap: 0.6rem; }
.doc-actions { display: inline-flex; gap: 0.4rem; }
.doc-actions button { border: 1px solid var(--color-border); background: transparent; color: var(--color-text-secondary); font-size: 0.78rem; padding: 0.15rem 0.55rem; border-radius: 5px; }
.issues { list-style: none; margin: 0.25rem 0 0; padding-left: 0.6rem; display: grid; gap: 0.2rem; color: var(--color-text-muted); font-size: 0.84rem; }
</style>
