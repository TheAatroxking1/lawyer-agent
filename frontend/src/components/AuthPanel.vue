<script setup lang="ts">
import { ref } from 'vue'

import { ApiError, apiClient, login } from '../api'
import { refreshAuth } from '../auth/state'
import { session } from '../auth/session'
import ErrorNote from './ErrorNote.vue'

type AuthMethod = 'wechat' | 'phone' | 'password'

const emit = defineEmits<{ success: []; close: []; 'go-register': [] }>()

const method = ref<AuthMethod>('password')
const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref<ApiError | null>(null)

async function submitPassword(): Promise<void> {
  if (busy.value) return
  error.value = null
  const name = username.value.trim()
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
    refreshAuth()
    emit('success')
  } catch (cause) {
    error.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    busy.value = false
  }
}

function pick(next: AuthMethod): void {
  error.value = null
  method.value = next
}
</script>

<template>
  <div class="auth-panel">
    <div class="tabs" role="tablist" aria-label="登录方式">
      <button
        type="button"
        :class="{ active: method === 'wechat' }"
        :aria-selected="method === 'wechat'"
        @click="pick('wechat')"
      >
        微信登录
      </button>
      <button
        type="button"
        :class="{ active: method === 'phone' }"
        :aria-selected="method === 'phone'"
        @click="pick('phone')"
      >
        手机号
      </button>
      <button
        type="button"
        :class="{ active: method === 'password' }"
        :aria-selected="method === 'password'"
        @click="pick('password')"
      >
        账号密码
      </button>
    </div>

    <div v-if="method === 'wechat'" class="not-open">
      <p class="not-open-title">微信扫码登录尚未开通</p>
      <p>
        接入微信开放平台的 OAuth 能力（AppID / AppSecret 与回调域名配置）后即可用
        微信扫码登录。当前请先使用「账号密码」登录。
      </p>
      <button type="button" class="ghost" disabled>微信扫码（暂未开通）</button>
    </div>

    <div v-else-if="method === 'phone'" class="not-open">
      <p class="not-open-title">手机号验证码登录尚未开通</p>
      <p>
        接入短信网关并支持手机号绑定后即可使用。当前请先使用「账号密码」登录，
        后续可在账号设置中绑定手机号。
      </p>
      <button type="button" class="ghost" disabled>获取验证码（暂未开通）</button>
    </div>

    <form v-else class="password-form" @submit.prevent="submitPassword">
      <ErrorNote :error="error" />
      <label>
        用户名
        <input
          v-model="username"
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
    </form>

    <p class="switch">
      还没有账号？
      <button type="button" class="link" @click="emit('go-register')">注册账号</button>
    </p>
  </div>
</template>

<style scoped>
.auth-panel {
  display: grid;
  gap: 1rem;
}
.tabs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.35rem;
  padding: 0.25rem;
  background: var(--color-bg-subtle);
  border-radius: 8px;
}
.tabs button {
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 0.9rem;
  border-radius: 6px;
  padding: 0.45rem 0.2rem;
}
.tabs button.active {
  background: var(--color-surface);
  color: var(--color-text);
  font-weight: 600;
  box-shadow: var(--shadow-card);
}
.not-open {
  display: grid;
  gap: 0.6rem;
  padding: 0.9rem 1rem;
  border: 1px dashed var(--color-border);
  border-radius: 8px;
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: 0.9rem;
  line-height: 1.6;
}
.not-open-title {
  margin: 0;
  font-weight: 600;
  color: var(--color-text);
}
.ghost {
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text-muted);
  justify-self: start;
}
.password-form {
  display: grid;
  gap: 0.85rem;
}
.password-form label {
  display: grid;
  gap: 0.3rem;
  font-size: 0.88rem;
  color: var(--color-text-secondary);
}
.switch {
  margin: 0;
  font-size: 0.85rem;
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
</style>
