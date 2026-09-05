<script setup lang="ts">
import { useRouter } from 'vue-router'

import { authState, refreshAuth } from '../auth/state'
import { session } from '../auth/session'

const router = useRouter()

function logout(): void {
  session.clearToken()
  refreshAuth()
  void router.replace('/chat')
}

function goLogin(): void {
  void router.push({ name: 'login', query: { next: router.currentRoute.value.fullPath } })
}
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <RouterLink class="brand" :to="{ name: 'chat' }">律师 Agent</RouterLink>
      <nav class="nav">
        <RouterLink :to="{ name: 'home' }" :class="{ active: $route.name === 'home' }">首页</RouterLink>
        <RouterLink :to="{ name: 'ask' }" :class="{ active: $route.name === 'ask' }">法规问答</RouterLink>
        <RouterLink :to="{ name: 'corpus' }" :class="{ active: $route.name === 'corpus' || $route.name === 'corpus-instrument' }">法规语料</RouterLink>
        <RouterLink :to="{ name: 'chat' }" :class="{ active: $route.name === 'chat' }">法规对话</RouterLink>
      </nav>
      <div v-if="authState.authenticated" class="account">
        <button class="ghost" type="button" @click="logout">退出登录</button>
      </div>
      <div v-else class="account">
        <button class="ghost" type="button" @click="router.push({ name: 'register' })">注册</button>
        <button class="ghost" type="button" @click="goLogin">登录</button>
      </div>
    </header>
    <main class="content">
      <RouterView />
    </main>
    <footer class="footer">律师 Agent · 法规数据与有据问答 · 界面内容不构成法律意见</footer>
  </div>
</template>

<style scoped>
.shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 1.25rem;
  padding: 0 1.25rem;
  height: 3.5rem;
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
}
.brand {
  font-weight: 700;
  font-size: 1.05rem;
  color: var(--color-text);
  text-decoration: none;
  white-space: nowrap;
}
.nav {
  display: flex;
  gap: 0.25rem;
  flex: 1;
  overflow-x: auto;
}
.nav a {
  padding: 0.35rem 0.7rem;
  border-radius: 6px;
  color: var(--color-text-secondary);
  text-decoration: none;
  font-size: 0.92rem;
  white-space: nowrap;
}
.nav a:hover {
  background: var(--color-bg-subtle);
}
.nav a.active {
  color: var(--color-accent);
  background: var(--color-accent-soft);
  font-weight: 600;
}
.account {
  display: flex;
  gap: 0.45rem;
}
.ghost {
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text-secondary);
  border-radius: 6px;
  padding: 0.35rem 0.8rem;
  cursor: pointer;
  font-size: 0.88rem;
  white-space: nowrap;
}
.ghost:hover {
  background: var(--color-bg-subtle);
}
.content {
  flex: 1;
  width: min(72rem, calc(100vw - 2rem));
  margin: 1.25rem auto;
}
.footer {
  padding: 0.9rem 1.25rem;
  border-top: 1px solid var(--color-border);
  color: var(--color-text-muted);
  font-size: 0.78rem;
}
</style>
