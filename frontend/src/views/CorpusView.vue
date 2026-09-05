<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiError, apiClient, listInstruments } from '../api'
import type { InstrumentSummary } from '../api'
import ErrorNote from '../components/ErrorNote.vue'

const PAGE_SIZE = 20

const items = ref<InstrumentSummary[]>([])
const nextBeforeId = ref<string | null>(null)
const history: string[] = []
const titleFilter = ref('')
const loading = ref(false)
const error = ref<ApiError | null>(null)
const searchedTitle = ref('')

async function load(beforeId: string | null, applyFilter: boolean): Promise<void> {
  error.value = null
  loading.value = true
  try {
    const page = await listInstruments(apiClient, {
      limit: PAGE_SIZE,
      before_id: beforeId,
      title: applyFilter ? searchedTitle.value || undefined : undefined,
    })
    items.value = page.items
    nextBeforeId.value = page.next_before_id
  } catch (cause) {
    error.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    loading.value = false
  }
}

function search(): void {
  history.length = 0
  searchedTitle.value = titleFilter.value.trim()
  void load(null, true)
}

function nextPage(): void {
  if (nextBeforeId.value === null) return
  history.push(String(nextBeforeId.value))
  void load(nextBeforeId.value, false)
}

function previousPage(): void {
  const before = history.pop()
  void load(before ?? null, false)
}

onMounted(() => {
  void load(null, true)
})
</script>

<template>
  <section class="corpus">
    <div class="head">
      <h1>法规语料</h1>
      <form class="search" @submit.prevent="search">
        <input
          v-model="titleFilter"
          placeholder="按名称检索（如：契税法）"
          aria-label="按名称检索"
        />
        <button type="submit" :disabled="loading">检索</button>
      </form>
    </div>
    <ErrorNote :error="error" context="语料加载失败" />
    <p v-if="loading" class="state">加载中…</p>
    <p v-else-if="items.length === 0 && !error" class="state">暂无匹配的法规。</p>
    <ul v-else class="list">
      <li v-for="item in items" :key="item.id" class="row">
        <div class="title-line">
          <strong>{{ item.title }}</strong>
          <span class="jurisdiction">{{ item.jurisdiction }}</span>
        </div>
        <div class="meta">
          <span>{{ item.issuing_authority }}</span>
          <span v-if="item.region_code">{{ item.region_code }}</span>
        </div>
      </li>
    </ul>
    <nav v-if="items.length > 0" class="pager">
      <button type="button" :disabled="history.length === 0" @click="previousPage">
        上一页
      </button>
      <button type="button" :disabled="nextBeforeId === null" @click="nextPage">
        下一页
      </button>
    </nav>
  </section>
</template>

<style scoped>
.corpus {
  display: grid;
  gap: 1rem;
}
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.head h1 {
  margin: 0;
}
.search {
  display: flex;
  gap: 0.5rem;
}
.search input {
  min-width: 14rem;
  padding: 0.45rem 0.7rem;
  border: 1px solid var(--color-border);
  border-radius: 6px;
}
.state {
  color: var(--color-text-muted);
}
.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.6rem;
}
.row {
  padding: 0.8rem 1rem;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
}
.title-line {
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
  flex-wrap: wrap;
}
.jurisdiction {
  color: var(--color-accent);
  font-size: 0.8rem;
  border: 1px solid var(--color-accent-soft-border);
  background: var(--color-accent-soft);
  padding: 0.05rem 0.45rem;
  border-radius: 999px;
}
.meta {
  display: flex;
  gap: 1rem;
  margin-top: 0.25rem;
  color: var(--color-text-muted);
  font-size: 0.84rem;
}
.pager {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}
</style>
