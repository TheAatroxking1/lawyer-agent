<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiError, apiClient, myTenants, switchTenant } from '../api'
import type { AccountTenant } from '../api'
import ErrorNote from '../components/ErrorNote.vue'
import { session } from '../auth/session'

const tenants = ref<AccountTenant[]>([])
const loading = ref(false)
const error = ref<ApiError | null>(null)
const switchingId = ref<string | null>(null)
const activeTenant = ref<AccountTenant | null>(null)
const switchError = ref<ApiError | null>(null)

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

function membershipLabel(value: string): string {
  return MEMBERSHIP_LABELS[value] ?? value
}

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
  switchError.value = null
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
        <p class="lead">租户级案件、文档上传登记与复核工作区。业务数据仅限本律所/团队可见。</p>
      </div>
      <button v-if="activeTenant" class="ghost" type="button" @click="leaveTenant">
        退出当前租户
      </button>
    </header>

    <ErrorNote :error="error" context="读取租户列表失败" />
    <p v-if="loading" class="state">加载中…</p>

    <section v-if="!loading && error === null && tenants.length === 0" class="guide">
      <h2>你还没有可用的律所/团队租户</h2>
      <p>
        案件与文档属于租户内部数据。加入流程（任选其一）：
      </p>
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
        <span class="code">{{ switchError.code }}</span>
        <span>{{ switchError.title }}</span>
      </p>
      <ul class="rows">
        <li v-for="tenant in tenants" :key="tenant.tenant_id" class="tenant-card">
          <div class="main">
            <strong>{{ tenant.name }}</strong>
            <span class="chip">{{ tenant.tenant_type }}</span>
          </div>
          <p class="meta">
            成员状态 <strong>{{ membershipLabel(tenant.membership_status) }}</strong>
            {{
              tenant.membership_status === 'active' && tenant.tenant_status === 'active'
                ? ''
                : ` · 租户状态 ${TENANT_LABELS[tenant.tenant_status] ?? tenant.tenant_status}`
            }}
          </p>
          <button
            v-if="tenant.membership_status === 'active' && tenant.tenant_status === 'active'"
            type="button"
            :disabled="switchingId !== null"
            @click="enterTenant(tenant)"
          >
            {{ switchingId === tenant.tenant_id ? '进入中…' : '进入工作台' }}
          </button>
          <p v-else class="pending">该成员/租户尚未生效，无法进入。</p>
        </li>
      </ul>
    </section>

    <section v-if="activeTenant" class="active">
      <h2>当前租户：{{ activeTenant.name }}</h2>
      <div class="capabilities">
        <div class="cap">
          <h3>上传登记</h3>
          <p>案件 Matter 与文档版本（DOCX 原件登记、白名单校验、复核状态机）端点已就绪。</p>
        </div>
        <div class="cap">
          <h3>风险检查与报告下载</h3>
          <p>Rule Check 运行后可下载 DOCX 报告；风险项人工处置状态机受审计。</p>
        </div>
        <div class="cap">
          <h3>说明</h3>
          <p>原始文件的二进制下载与 MinIO 预签名直传属于后续切片；本页先打通租户上下文。</p>
        </div>
      </div>
    </section>
  </section>
</template>

<style scoped>
.workbench {
  display: grid;
  gap: 1.1rem;
  max-width: 54rem;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}
.head h1 {
  margin: 0;
}
.lead {
  margin: 0.3rem 0 0;
  color: var(--color-text-muted);
  font-size: 0.88rem;
}
.ghost {
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.state {
  color: var(--color-text-muted);
}
.guide {
  border: 1px dashed var(--color-border);
  border-radius: 10px;
  padding: 1rem 1.25rem;
  background: var(--color-surface);
}
.guide h2,
.tenant-list h2,
.active h2 {
  margin: 0 0 0.6rem;
  font-size: 1.05rem;
}
.guide ol {
  display: grid;
  gap: 0.4rem;
  margin: 0.4rem 0;
  padding-left: 1.3rem;
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.note {
  margin: 0.6rem 0 0;
  color: var(--color-text-muted);
  font-size: 0.85rem;
}
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.7rem;
}
.tenant-card {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 0.9rem 1rem;
  background: var(--color-surface);
  display: grid;
  gap: 0.35rem;
}
.main {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}
.chip {
  border: 1px solid var(--color-accent-soft-border);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  border-radius: 999px;
  padding: 0.02rem 0.5rem;
  font-size: 0.78rem;
}
.meta {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 0.85rem;
}
.pending {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 0.85rem;
}
.error {
  display: flex;
  gap: 0.5rem;
  margin: 0 0 0.5rem;
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
.capabilities {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  gap: 0.8rem;
}
.cap {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 0.85rem 1rem;
  background: var(--color-surface);
}
.cap h3 {
  margin: 0 0 0.35rem;
  font-size: 0.98rem;
}
.cap p {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 0.88rem;
  line-height: 1.55;
}
</style>
