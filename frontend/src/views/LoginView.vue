<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'

import AuthPanel from '../components/AuthPanel.vue'

const router = useRouter()
const route = useRoute()

function goRegister(): void {
  void router.push('/register')
}

function onSuccess(): void {
  const next = typeof route.query.next === 'string' ? route.query.next : '/chat'
  void router.replace(next)
}
</script>

<template>
  <main class="auth-page">
    <section class="auth-card">
      <h1>登录</h1>
      <p class="hint">微信 / 手机号 / 账号密码</p>
      <AuthPanel @success="onSuccess" @go-register="goRegister" />
    </section>
  </main>
</template>

<style scoped>
.auth-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  background: var(--color-bg-subtle);
}
.auth-card {
  width: min(22rem, calc(100vw - 2rem));
  display: grid;
  gap: 0.9rem;
  padding: 2rem;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  box-shadow: var(--shadow-card);
}
h1 {
  margin: 0;
  font-size: 1.35rem;
}
.hint {
  margin: -0.4rem 0 0.4rem;
  color: var(--color-text-muted);
  font-size: 0.85rem;
}
</style>
