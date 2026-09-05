<script setup lang="ts"></script>

<template>
  <section class="chat">
    <h1>法规对话</h1>
    <p class="lead">
      后端入口 <code>POST /api/v1/legal/chat</code> 已就绪（经 ModelGateway +
      DeepSeek provider + 调用记录器）。对话界面与流式（SSE）问答将在后续切片接入。
    </p>
    <div class="hint-box">
      <h2>启用对话前的唯一手动步骤</h2>
      <ol>
        <li>
          复制 <code>deploy/deepseek.config.example.json</code> 为
          <code>deploy/deepseek.config.json</code>；
        </li>
        <li>在其中填入你自己的 DeepSeek API Key（该文件不入 Git）；</li>
        <li>
          以 <code>LAWYER_DEEPSEEK_API_KEY_FILE</code> 指向该文件（绝对路径）启动后端。
        </li>
      </ol>
      <p class="note">未配置 Key 时接口返回 503（model_provider_unavailable），不会伪造回复。</p>
    </div>
  </section>
</template>

<style scoped>
.chat {
  display: grid;
  gap: 1rem;
  max-width: 44rem;
}
h1 {
  margin: 0;
}
.lead {
  color: var(--color-text-secondary);
  line-height: 1.6;
}
code {
  font-family: var(--font-mono);
  background: var(--color-bg-subtle);
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
  font-size: 0.88em;
}
.hint-box {
  border: 1px dashed var(--color-border);
  border-radius: 10px;
  padding: 1rem 1.25rem;
}
.hint-box h2 {
  margin: 0 0 0.5rem;
  font-size: 1rem;
}
.hint-box ol {
  margin: 0;
  padding-left: 1.25rem;
  display: grid;
  gap: 0.4rem;
  color: var(--color-text-secondary);
}
.note {
  margin: 0.8rem 0 0;
  color: var(--color-text-muted);
  font-size: 0.85rem;
}
</style>
