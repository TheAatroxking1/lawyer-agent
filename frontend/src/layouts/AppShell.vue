<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import { authState } from '../auth/state'
import { logoutSession } from '../auth/transport'
import ConversationHistory from '../components/ConversationHistory.vue'

const router = useRouter()
const collapsed = ref(false)
const mobileOpen = ref(false)
const conversationKey = ref(0)
const logoutError = ref('')
const loggingOut = ref(false)
const isConversation = computed(() => ['chat', 'contract-review'].includes(String(router.currentRoute.value.name)))
function newConversation(): void {
  conversationKey.value += 1
  mobileOpen.value = false
  void router.push('/chat')
}

async function logout(): Promise<void> {
  if (loggingOut.value) return
  loggingOut.value = true
  logoutError.value = ''
  try {
    await logoutSession()
    conversationKey.value += 1
    await router.replace('/chat')
  } catch { logoutError.value = '退出未完成，请检查连接后重试。' }
  finally { loggingOut.value = false }
}

function goLogin(): void {
  void router.push({ name: 'login', query: { next: router.currentRoute.value.fullPath } })
}
</script>

<template>
  <div class="shell" :class="{ collapsed, 'mobile-open': mobileOpen }">
    <header class="mobile-header"><button aria-label="展开导航" @click="mobileOpen = !mobileOpen">☰</button><span>律师 Agent</span><button aria-label="新对话" @click="newConversation">＋</button></header>
    <button v-if="mobileOpen" class="mobile-backdrop" aria-label="关闭导航" @click="mobileOpen = false" />
    <aside class="sidebar">
      <div class="sidebar-brand"><RouterLink to="/chat" aria-label="律师 Agent 首页"><span class="brand-mark">✳</span><strong>律师 Agent</strong></RouterLink><button class="collapse-button" :aria-label="collapsed ? '展开侧栏' : '收起侧栏'" @click="collapsed = !collapsed">◫</button></div>
      <button class="new-chat" @click="newConversation"><span>＋</span><strong>新对话</strong></button>
      <nav class="primary-nav" aria-label="功能导航" @click="mobileOpen = false">
        <RouterLink to="/chat" :class="{ active: $route.name === 'chat' }" title="对话"><span>◌</span><strong>对话</strong></RouterLink>
        <RouterLink to="/contract-review" :class="{ active: $route.name === 'contract-review' }" title="合同审阅"><span>▤</span><strong>合同审阅</strong></RouterLink>
      </nav>
      <div class="nav-caption">工作空间</div>
      <nav class="secondary-nav" aria-label="工作空间功能" @click="mobileOpen = false">
        <RouterLink to="/ask" title="法规问答"><span>⌕</span><strong>法规问答</strong></RouterLink>
        <RouterLink to="/corpus" title="法规语料"><span>▥</span><strong>法规语料</strong></RouterLink>
        <RouterLink to="/documents" title="案件文档"><span>▱</span><strong>案件文档</strong></RouterLink>
        <RouterLink to="/agent-tools" title="智能工具"><span>◇</span><strong>智能工具</strong></RouterLink>
        <RouterLink to="/home" title="工作台"><span>⊞</span><strong>工作台</strong></RouterLink>
      </nav>
      <ConversationHistory v-if="!collapsed" :tenant-id="authState.activeTenant?.tenant_id ?? ''" :identity="authState.accountIdentity ?? ''" @navigate="mobileOpen = false" />
      <div class="sidebar-bottom">
        <p v-if="logoutError" role="alert">{{ logoutError }}</p>
        <div v-if="authState.authenticated" class="account-row"><div class="account-avatar">我</div><div class="account-copy"><strong>{{ authState.activeTenant?.name || '我的工作空间' }}</strong><small>已登录 · 自动续期</small></div><button class="account-action" title="退出登录" :disabled="loggingOut" @click="logout">{{ loggingOut ? '退出中' : '退出' }}</button></div>
        <div v-else class="guest-actions"><button @click="goLogin">登录</button><button @click="router.push({ name: 'register' })">注册</button></div>
      </div>
    </aside>
    <main class="content" :class="{ 'conversation-content': isConversation }"><RouterView :key="String($route.name) + conversationKey" /></main>
  </div>
</template>

<style scoped>
.shell { min-height: 100dvh; display: grid; grid-template-columns: 244px minmax(0, 1fr); background: white; }
.sidebar { position: sticky; top: 0; height: 100dvh; overflow-y: auto; display: flex; flex-direction: column; padding: 22px 14px 14px; background: #f5f5f3; border-right: 1px solid #eeeeeb; color: #3b3b37; }
.sidebar-brand { display: flex; align-items: center; justify-content: space-between; margin: 0 4px 27px; }.sidebar-brand a { display: flex; align-items: center; gap: 10px; text-decoration: none; color: inherit; }.sidebar-brand strong { font-weight: 600; font-size: 15px; letter-spacing: .2px; }.brand-mark { color: #365548; font-size: 29px; line-height: 1; }.collapse-button { border: 0; background: transparent; padding: 4px 7px; color: #858581; font-size: 21px; }
.new-chat { display: flex; align-items: center; gap: 10px; width: 100%; padding: 11px 13px; background: #fff; color: #383833; border: 1px solid #dfdfdb; border-radius: 10px; margin-bottom: 17px; box-shadow: 0 1px 2px #00000003; }.new-chat span { font-size: 21px; line-height: 1; }.new-chat strong { font-size: 13px; font-weight: 500; }
nav { display: grid; gap: 4px; }nav a { display: flex; align-items: center; gap: 12px; padding: 10px 13px; color: #6b6b64; text-decoration: none; border-radius: 8px; font-size: 13px; }nav a strong { font-weight: 400; }nav a span { font-size: 19px; width: 20px; text-align: center; line-height: 1; }nav a:hover { background: #ebebe7; }nav a.active, nav a.router-link-exact-active { background: #e8eae4; color: #3d5142; }nav a.active strong, nav a.router-link-exact-active strong { font-weight: 550; }.nav-caption { font-size: 11px; letter-spacing: 1px; color: #a1a199; margin: 30px 13px 12px; }
.sidebar-bottom { margin-top: auto; }.sidebar-note { margin: 20px 7px; padding: 16px 12px; border: 1px solid #e4e4de; border-radius: 12px; }.note-symbol { font-size: 19px; color: #718166; display: block; margin-bottom: 9px; }.sidebar-note strong { font-size: 12px; font-weight: 500; color: #74796b; }.sidebar-note p { font-size: 11px; color: #a0a195; margin: 5px 0 0; }.account-row { display: flex; align-items: center; gap: 10px; padding: 13px 7px 2px; border-top: 1px solid #e4e4df; }.account-avatar { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 50%; background: #e2e4db; font-size: 12px; color: #66745c; }.account-copy { flex: 1; }.account-copy strong { display: block; font-size: 12px; font-weight: 500; }.account-copy small { font-size: 10px; color: #999b90; }.account-action { padding: 2px; border: 0; background: transparent; color: #999; font-size: 11px; }.guest-actions { display: flex; gap: 8px; }.guest-actions button { flex: 1; background: white; color: #666; border: 1px solid #deded7; }
.content { min-width: 0; padding: 30px; }.conversation-content { padding: 0; }.mobile-header, .mobile-backdrop { display: none; }
.shell.collapsed { grid-template-columns: 70px minmax(0, 1fr); }.collapsed .sidebar { padding: 22px 8px 14px; }.collapsed .sidebar-brand { flex-direction: column; gap: 12px; }.collapsed .sidebar-brand strong, .collapsed nav strong, .collapsed .new-chat strong, .collapsed .nav-caption, .collapsed .sidebar-note, .collapsed .account-copy, .collapsed .account-action { display: none; }.collapsed .new-chat, .collapsed nav a { justify-content: center; padding: 12px; }.collapsed .secondary-nav { margin-top: 28px; }.collapsed .guest-actions { flex-direction: column; }.collapsed .guest-actions button { font-size: 11px; padding: 6px; }
@media (max-width: 760px) { .shell, .shell.collapsed { display: block; padding-top: 52px; }.mobile-header { display: flex; align-items: center; justify-content: space-between; position: fixed; inset: 0 0 auto; height: 52px; padding: 0 14px; background: #fafaf8; border-bottom: 1px solid #ecece8; z-index: 35; font-size: 14px; }.mobile-header button { border: 0; background: transparent; color: #555; font-size: 21px; padding: 5px; }.sidebar { display: none; }.mobile-open .sidebar { display: flex; position: fixed; top: 52px; left: 0; width: 244px; height: calc(100dvh - 52px); z-index: 40; }.mobile-backdrop { display: block; position: fixed; inset: 52px 0 0; border: 0; border-radius: 0; background: #0005; z-index: 39; }.content { padding: 18px; }.conversation-content { padding: 0; } }
</style>
