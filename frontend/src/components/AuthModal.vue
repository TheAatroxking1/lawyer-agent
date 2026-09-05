<script setup lang="ts">
import AuthPanel from './AuthPanel.vue'

defineEmits<{ success: []; close: [] }>()
</script>

<template>
  <Teleport to="body">
    <div class="overlay" role="presentation" @click.self="$emit('close')">
      <section
        class="modal"
        role="dialog"
        aria-modal="true"
        aria-label="登录"
        @keydown.esc="$emit('close')"
      >
        <header class="head">
          <h2>登录后继续对话</h2>
          <button type="button" class="close" aria-label="关闭" @click="$emit('close')">×</button>
        </header>
        <AuthPanel @success="$emit('success')" @close="$emit('close')" />
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: grid;
  place-items: center;
  background: rgba(15, 20, 32, 0.45);
  padding: 1rem;
}
.modal {
  width: min(22rem, 100%);
  background: var(--color-surface);
  border-radius: 12px;
  padding: 1.4rem 1.4rem 1.2rem;
  box-shadow: 0 12px 40px rgba(15, 20, 32, 0.28);
  display: grid;
  gap: 1rem;
}
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.head h2 {
  margin: 0;
  font-size: 1.1rem;
}
.close {
  border: none;
  background: none;
  color: var(--color-text-muted);
  font-size: 1.4rem;
  line-height: 1;
  padding: 0.1rem 0.4rem;
  cursor: pointer;
}
</style>
