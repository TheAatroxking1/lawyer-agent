<script setup lang="ts"></script>

<template>
  <section class="not-found">
    <h1>404</h1>
    <p>页面不存在。</p>
    <RouterLink to="/">返回首页</RouterLink>
  </section>
</template>

<style scoped>
.not-found {
  min-height: 60vh;
  display: grid;
  place-content: center;
  text-align: center;
  gap: 0.4rem;
}
.not-found h1 {
  margin: 0;
  font-size: 3rem;
}
</style>
