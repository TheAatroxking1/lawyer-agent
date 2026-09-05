<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError, apiClient, login } from '../api'
import ErrorNote from '../components/ErrorNote.vue'
import { session } from '../auth/session'

const router = useRouter()
const route = useRoute()

const identifier = ref('')
const password = ref('')
const busy = ref(false)
const error = ref<ApiError | null>(null)

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = null
  const name = identifier.value.trim()
  if (name.length === 0 || password.value.length === 0) {
    error.value = new ApiError({
      status: 422,
      code: 'request_validation_failed',
      title: '请输入用户名与密码',
    })
    return
  }
  busy.value = true
  try {
    const result = await login(apiClient, {
      kind: 'username',
      identifier: name,
      password: password.value,
    })
    session.saveToken(result.access_token)
    const next = typeof route.query.next === 'string' ? route.query.next : '/'
    await router.replace(next)
  } catch (cause) {
    error.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <form class="auth-card" @submit.prevent="submit">
      <h1>登录</h1>
      <p class="hint">企业法务 / 律所工作台</p>
      <ErrorNote :error="error" />
      <label>
        用户名
        <input
          v-model="identifier"
          name="username"
          autocomplete="username"
          :disabled="busy"
          required
        />
      </label>
      <label>
        密码
        <input
          v-model="password"
          name="password"
          type="password"
          autocomplete="current-password"
          :disabled="busy"
          required
        />
      </label>
      <button type="submit" :disabled="busy">{{ busy ? '登录中…' : '登录' }}</button>
      <p class="switch">
        还没有账号？
        <RouterLink to="/register">注册</RouterLink>
      </p>
    </form>
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
label {
  display: grid;
  gap: 0.3rem;
  font-size: 0.88rem;
  color: var(--color-text-secondary);
}
.switch {
  font-size: 0.85rem;
  color: var(--color-text-muted);
}
</style>
