<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError, apiClient, registerAccount } from '../api'
import { refreshAuth } from '../auth/state'
import { session } from '../auth/session'
import ErrorNote from '../components/ErrorNote.vue'

const router = useRouter()

const username = ref('')
const displayName = ref('')
const password = ref('')
const busy = ref(false)
const error = ref<ApiError | null>(null)

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = null
  if (
    username.value.trim().length === 0 ||
    displayName.value.trim().length === 0 ||
    password.value.length === 0
  ) {
    error.value = new ApiError({
      status: 422,
      code: 'request_validation_failed',
      title: '请填写完整：用户名、显示名与密码',
    })
    return
  }
  busy.value = true
  try {
    const result = await registerAccount(apiClient, {
      username: username.value.trim(),
      display_name: displayName.value.trim(),
      password: password.value,
    })
    session.saveToken(result.access_token)
    refreshAuth()
    await router.replace('/chat')
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
      <h1>注册账号</h1>
      <p class="hint">公共语料与法规问答使用平台账号登录后即可访问</p>
      <ErrorNote :error="error" />
      <label>
        用户名
        <input v-model="username" name="username" autocomplete="username" :disabled="busy" required />
      </label>
      <label>
        显示名
        <input v-model="displayName" name="display_name" autocomplete="name" :disabled="busy" required />
      </label>
      <label>
        密码
        <input
          v-model="password"
          name="password"
          type="password"
          autocomplete="new-password"
          :disabled="busy"
          required
        />
      </label>
      <button type="submit" :disabled="busy">{{ busy ? '注册中…' : '注册并登录' }}</button>
      <p class="switch">
        已有账号？
        <RouterLink to="/login">登录</RouterLink>
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
