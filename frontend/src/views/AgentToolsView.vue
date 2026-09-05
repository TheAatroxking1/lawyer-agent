<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiError, apiClient, callAgentTool, listAgentTools } from '../api'
import type { AgentToolInfo } from '../api'
import ErrorNote from '../components/ErrorNote.vue'

const tools = ref<AgentToolInfo[]>([])
const loading = ref(false)
const error = ref<ApiError | null>(null)
const runningName = ref<string | null>(null)
const resultText = ref<string | null>(null)
const resultError = ref<ApiError | null>(null)

async function load(): Promise<void> {
  error.value = null
  loading.value = true
  try {
    tools.value = await listAgentTools(apiClient)
  } catch (cause) {
    error.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    loading.value = false
  }
}

async function run(tool: AgentToolInfo): Promise<void> {
  if (runningName.value !== null) return
  resultError.value = null
  resultText.value = null
  runningName.value = tool.name
  try {
    const result = await callAgentTool(apiClient, { tool: tool.name, args: {} })
    if (!result.ok) {
      throw new ApiError({
        status: 200,
        code: result.error_code ?? 'agent_tool_failed',
        title: result.error_message ?? '工具调用失败',
      })
    }
    resultText.value = JSON.stringify(result.output, null, 2)
  } catch (cause) {
    resultError.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    runningName.value = null
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <section class="agent">
    <header>
      <h1>智能工具</h1>
      <p class="lead">
        通过受控 Agent 网关（MCP Client Gateway）列出并调用白名单工具。只有网关登记的
        工具可运行，参数经 Schema 校验；当前内置能力为工具清单自省。
      </p>
    </header>

    <ErrorNote :error="error" context="加载工具失败" />
    <p v-if="loading" class="state">加载中…</p>

    <ul v-if="tools.length > 0" class="rows">
      <li v-for="tool in tools" :key="tool.name" class="card">
        <div class="main">
          <code>{{ tool.name }}</code>
        </div>
        <p class="desc">{{ tool.description }}</p>
        <button type="button" :disabled="runningName !== null" @click="run(tool)">
          {{ runningName === tool.name ? '调用中…' : '调用' }}
        </button>
      </li>
    </ul>

    <ErrorNote :error="resultError" context="工具调用失败" />
    <pre v-if="resultText" class="result">{{ resultText }}</pre>
  </section>
</template>

<style scoped>
.agent {
  display: grid;
  gap: 1rem;
  max-width: 48rem;
}
header h1 {
  margin: 0;
}
.lead {
  margin: 0.3rem 0 0;
  color: var(--color-text-muted);
  font-size: 0.88rem;
}
.state {
  color: var(--color-text-muted);
}
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.7rem;
}
.card {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 0.9rem 1rem;
  background: var(--color-surface);
  display: grid;
  gap: 0.4rem;
  justify-items: start;
}
.main code {
  font-family: var(--font-mono);
  font-weight: 600;
  background: var(--color-bg-subtle);
  padding: 0.1rem 0.45rem;
  border-radius: 4px;
}
.desc {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 0.9rem;
}
.result {
  max-height: 20rem;
  overflow: auto;
  padding: 0.8rem 1rem;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg-subtle);
  font-size: 0.82rem;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
