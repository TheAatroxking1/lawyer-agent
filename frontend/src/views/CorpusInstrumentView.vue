<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { ApiError, apiClient, getInstrument, getVersion, listProvisionsForVersion, listVersionsForInstrument } from '../api'
import type { InstrumentSummary, LegalVersionSummary, ProvisionSummary } from '../api'
import ErrorNote from '../components/ErrorNote.vue'
import { isoDate, legalCategoryLabel, versionStatusLabel } from '../lib/format'

const route = useRoute()
const instrumentId = computed(() => String(route.params.instrumentId ?? ''))

const instrument = ref<InstrumentSummary | null>(null)
const versions = ref<LegalVersionSummary[]>([])
const selectedVersionId = ref<string | null>(null)
const selectedVersion = ref<LegalVersionSummary | null>(null)
const provisions = ref<ProvisionSummary[]>([])
const loadingVersions = ref(false)
const loadingProvisions = ref(false)
const error = ref<ApiError | null>(null)

async function loadVersions(): Promise<void> {
  error.value = null
  loadingVersions.value = true
  try {
    const [identity, allVersions] = await Promise.all([
      getInstrument(apiClient, instrumentId.value),
      listVersionsForInstrument(apiClient, instrumentId.value),
    ])
    instrument.value = identity
    versions.value = allVersions
    const target = allVersions[0] ?? null
    selectedVersionId.value = target ? target.id : null
  } catch (cause) {
    error.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    loadingVersions.value = false
  }
}

async function loadSelected(versionId: string): Promise<void> {
  error.value = null
  loadingProvisions.value = true
  try {
    const [meta, items] = await Promise.all([
      getVersion(apiClient, versionId),
      listProvisionsForVersion(apiClient, versionId),
    ])
    selectedVersion.value = meta
    provisions.value = items
  } catch (cause) {
    error.value =
      cause instanceof ApiError
        ? cause
        : new ApiError({ status: 0, code: 'network_error', title: '无法连接服务，请稍后重试' })
  } finally {
    loadingProvisions.value = false
  }
}

watch(
  selectedVersionId,
  (id) => {
    if (id) void loadSelected(id)
  },
  { immediate: true },
)

watch(
  instrumentId,
  () => {
    void loadVersions()
  },
  { immediate: true },
)
</script>

<template>
  <section class="detail">
    <RouterLink class="back" to="/corpus">← 返回语料目录</RouterLink>
    <ErrorNote :error="error" context="加载失败" />

    <template v-if="instrument">
      <header class="header">
        <h1>{{ instrument.title }}</h1>
        <p class="meta">
          <span>{{ instrument.issuing_authority }}</span>
          <span class="chip">{{ legalCategoryLabel(instrument.category) }}</span>
          <span class="chip">{{ instrument.jurisdiction }}</span>
          <span v-if="instrument.region_code" class="chip">{{ instrument.region_code }}</span>
        </p>
      </header>

      <section v-if="loadingVersions" class="state">版本加载中…</section>
      <section v-else-if="versions.length === 0" class="state">该法规暂无已入库版本。</section>
      <template v-else>
        <div class="versions" role="tablist" aria-label="法规版本">
          <button
            v-for="version in versions"
            :key="version.id"
            type="button"
            class="version-tab"
            :class="{ active: version.id === selectedVersionId }"
            :aria-selected="version.id === selectedVersionId"
            @click="selectedVersionId = version.id"
          >
            {{ version.version_label }}
            <span class="status" :class="version.status">{{ versionStatusLabel(version.status) }}</span>
          </button>
        </div>

        <section v-if="selectedVersion" class="version-meta">
          <dl>
            <div>
              <dt>状态</dt>
              <dd>{{ versionStatusLabel(selectedVersion.status) }}</dd>
            </div>
            <div v-if="selectedVersion.published_on">
              <dt>公布日期</dt>
              <dd>{{ isoDate(selectedVersion.published_on) }}</dd>
            </div>
            <div v-if="selectedVersion.effective_on">
              <dt>施行日期</dt>
              <dd>{{ isoDate(selectedVersion.effective_on) }}</dd>
            </div>
            <div v-if="selectedVersion.repealed_on">
              <dt>废止日期</dt>
              <dd>{{ isoDate(selectedVersion.repealed_on) }}</dd>
            </div>
            <div v-if="selectedVersion.law_number">
              <dt>文号</dt>
              <dd>{{ selectedVersion.law_number }}</dd>
            </div>
            <div v-if="selectedVersion.dataset_version">
              <dt>数据集</dt>
              <dd>{{ selectedVersion.dataset_version }}</dd>
            </div>
            <div v-if="selectedVersion.source_ref">
              <dt>来源</dt>
              <dd class="mono">{{ selectedVersion.source_ref }}</dd>
            </div>
          </dl>
        </section>

        <section class="provisions">
          <h2>条文正文</h2>
          <p v-if="loadingProvisions" class="state">条文加载中…</p>
          <p v-else-if="provisions.length === 0" class="state">该版本暂无条文。</p>
          <ol v-else class="provision-list">
            <li v-for="provision in provisions" :key="provision.id" class="provision">
              <h3 v-if="provision.title">{{ provision.title }}</h3>
              <p class="provision-no">{{ provision.provision_no }}</p>
              <p class="full-text">{{ provision.full_text }}</p>
            </li>
          </ol>
        </section>
      </template>
    </template>
  </section>
</template>

<style scoped>
.detail {
  display: grid;
  gap: 1rem;
}
.back {
  justify-self: start;
  font-size: 0.88rem;
  text-decoration: none;
}
.header h1 {
  margin: 0;
}
.meta {
  display: flex;
  gap: 0.6rem;
  align-items: center;
  color: var(--color-text-muted);
  font-size: 0.9rem;
  margin: 0.4rem 0 0;
}
.chip {
  border: 1px solid var(--color-accent-soft-border);
  background: var(--color-accent-soft);
  color: var(--color-accent);
  border-radius: 999px;
  padding: 0.05rem 0.5rem;
  font-size: 0.8rem;
}
.state {
  color: var(--color-text-muted);
}
.versions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.version-tab {
  display: inline-flex;
  gap: 0.45rem;
  align-items: center;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  border-radius: 999px;
  padding: 0.35rem 0.9rem;
  font-size: 0.9rem;
}
.version-tab:hover {
  border-color: var(--color-accent);
}
.version-tab.active {
  border-color: var(--color-accent);
  background: var(--color-accent-soft);
  font-weight: 600;
}
.status {
  font-size: 0.74rem;
  padding: 0.05rem 0.45rem;
  border-radius: 999px;
  background: var(--color-bg-subtle);
}
.status.repealed {
  background: var(--color-danger-bg);
  color: var(--color-danger-text);
}
.version-meta dl {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
  gap: 0.6rem 1.2rem;
  margin: 0;
  padding: 1rem;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-surface);
}
.version-meta dt {
  color: var(--color-text-muted);
  font-size: 0.78rem;
}
.version-meta dd {
  margin: 0.1rem 0 0;
  font-size: 0.92rem;
}
.mono {
  font-family: var(--font-mono);
  font-size: 0.82rem;
  word-break: break-all;
}
.provisions h2 {
  font-size: 1.05rem;
  margin: 0.5rem 0 0;
}
.provision-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.7rem;
}
.provision {
  padding: 0.9rem 1.1rem;
  border: 1px solid var(--color-border);
  border-left: 4px solid var(--color-accent);
  border-radius: 8px;
  background: var(--color-surface);
}
.provision h3 {
  margin: 0 0 0.3rem;
  font-size: 0.98rem;
}
.provision-no {
  margin: 0 0 0.3rem;
  color: var(--color-text-muted);
  font-size: 0.82rem;
  font-weight: 600;
}
.full-text {
  margin: 0;
  white-space: pre-wrap;
  line-height: 1.7;
  font-size: 0.95rem;
}
</style>
