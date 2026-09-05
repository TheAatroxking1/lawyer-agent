<script setup lang="ts">
import type { ApiError } from '../api/client'

defineProps<{
  error: ApiError | null
  context?: string
}>()
</script>

<template>
  <div v-if="error" class="error-note" role="alert">
    <span class="error-code">{{ error.code }}</span>
    <span>{{ context ?? error.title }}</span>
  </div>
</template>

<style scoped>
.error-note {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
  padding: 0.6rem 0.8rem;
  border: 1px solid var(--color-danger-border);
  border-radius: 6px;
  background: var(--color-danger-bg);
  color: var(--color-danger-text);
  font-size: 0.9rem;
}
.error-code {
  font-family: var(--font-mono);
  font-size: 0.78rem;
  opacity: 0.85;
}
</style>
