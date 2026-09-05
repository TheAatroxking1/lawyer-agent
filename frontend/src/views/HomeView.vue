<script setup lang="ts"></script>

<template>
  <section class="home">
    <h1>欢迎使用律师 Agent</h1>
    <p class="lead">
      面向企业法务、律所及其客户的一站式工作台。当前骨架已接通：账号登录、法规语料目录浏览。
    </p>
    <div class="cards">
      <RouterLink class="card" to="/corpus">
        <h2>法规语料</h2>
        <p>浏览已入库的公共法律法规目录，按名称、发布机关检索。</p>
      </RouterLink>
      <RouterLink class="card" to="/chat">
        <h2>法规对话</h2>
        <p>基于 DeepSeek 的公共问答，多轮会话（配置 API Key 后可用）。</p>
      </RouterLink>
      <div class="card muted">
        <h2>更多能力</h2>
        <p>版本条文详情、证据检索问答、文档上传下载等能力正在接入中。</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.home {
  display: grid;
  gap: 1rem;
}
h1 {
  margin: 0;
}
.lead {
  margin: 0;
  color: var(--color-text-secondary);
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
  gap: 1rem;
  margin-top: 0.5rem;
}
.card {
  display: block;
  padding: 1.1rem;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  text-decoration: none;
  color: inherit;
  background: var(--color-surface);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
a.card:hover {
  border-color: var(--color-accent);
  box-shadow: var(--shadow-card);
}
.card h2 {
  margin: 0 0 0.4rem;
  font-size: 1.02rem;
}
.card p {
  margin: 0;
  color: var(--color-text-muted);
  font-size: 0.88rem;
  line-height: 1.5;
}
.muted {
  cursor: default;
}
</style>
