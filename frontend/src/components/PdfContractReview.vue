<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentLoadingTask,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist'

import {
  boundedCanvasScale,
  createAsyncGenerationGuard,
  overlayPercent,
} from '../api/contractReview'
import type { ContractReviewRun, ReviewIssue } from '../api/contractReview'

GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString()

const props = defineProps<{
  pdfBytes: Uint8Array | null
  run: ContractReviewRun | null
}>()

interface Overlay {
  issue: ReviewIssue
  anchorId: string
  page: number
  left: number
  top: number
  width: number
  height: number
}

const pdf = shallowRef<PDFDocumentProxy | null>(null)
const loadingTask = shallowRef<PDFDocumentLoadingTask | null>(null)
const renderTasks = new Map<number, RenderTask>()
const canvases = new Map<number, HTMLCanvasElement>()
const scales = ref<number[]>([])
const trustedDimensions = ref<boolean[]>([])
const pageCount = ref(0)
const pageStart = ref(1)
const PAGE_WINDOW = 3
const selected = ref<ReviewIssue | null>(null)
const previewError = ref('')
let renderGeneration = 0
const loadGuard = createAsyncGenerationGuard()
let mounted = true
const visiblePages = computed(() => {
  const end = Math.min(pageCount.value, pageStart.value + PAGE_WINDOW - 1)
  return Array.from({ length: Math.max(0, end - pageStart.value + 1) }, (_, index) => pageStart.value + index)
})

function canvasRef(page: number, element: unknown): void {
  if (element instanceof HTMLCanvasElement) canvases.set(page, element)
  else canvases.delete(page)
}

function validDimension(value: unknown): value is [number, number] {
  return Array.isArray(value) && value.length === 2 && value.every(
    (item) => typeof item === 'number' && Number.isFinite(item) && item > 0,
  )
}

const overlays = computed<Overlay[]>(() => {
  if (!props.run || !props.pdfBytes) return []
  const anchors = new Map(
    props.run.blocks.flatMap((block) => block.anchors.map((anchor) => [
      anchor.anchor_id, { anchor, blockId: block.block_id },
    ])),
  )
  const result: Overlay[] = []
  for (const issue of props.run.issues) {
    const highlights = issue.highlights?.length
      ? issue.highlights
      : issue.anchor_id === null
        ? []
        : [{ block_id: issue.block_id, anchor_id: issue.anchor_id, start: issue.start, end: issue.end }]
    for (const highlight of highlights) {
      const entry = anchors.get(highlight.anchor_id)
      if (!entry || entry.blockId !== highlight.block_id) continue
      const { anchor } = entry
      if (highlight.start < anchor.start || highlight.end > anchor.end || highlight.end <= highlight.start) continue
      if (anchor.page < 1 || anchor.page > props.run.page_dimensions.length) continue
      if (!trustedDimensions.value[anchor.page - 1]) continue
      const dimension = props.run.page_dimensions[anchor.page - 1]
      if (!validDimension(dimension) || anchor.quad.length !== 8 || !anchor.quad.every(Number.isFinite)) continue
      const xs = [anchor.quad[0], anchor.quad[2], anchor.quad[4], anchor.quad[6]]
      const ys = [anchor.quad[1], anchor.quad[3], anchor.quad[5], anchor.quad[7]]
      const left = Math.min(...xs)
      const right = Math.max(...xs)
      const top = Math.min(...ys)
      const bottom = Math.max(...ys)
      if (left < 0 || top < 0 || right > dimension[0] || bottom > dimension[1] || right <= left || bottom <= top) continue
      result.push({ issue, anchorId: anchor.anchor_id, page: anchor.page, left, top, width: right - left, height: bottom - top })
    }
  }
  return result
})

function pageOverlays(page: number): Overlay[] {
  return overlays.value.filter((overlay) => overlay.page === page)
}

function overlayStyle(overlay: Overlay): Record<string, string> {
  const dimension = props.run?.page_dimensions[overlay.page - 1]
  if (!dimension) return { display: 'none' }
  return overlayPercent(
    overlay.left, overlay.top, overlay.width, overlay.height, dimension[0], dimension[1],
  )
}

async function resetPdfState(): Promise<void> {
  selected.value = null
  previewError.value = ''
  for (const task of renderTasks.values()) task.cancel()
  renderTasks.clear()
  canvases.clear()
  const oldLoading = loadingTask.value
  loadingTask.value = null
  pdf.value = null
  pageCount.value = 0
  pageStart.value = 1
  scales.value = []
  trustedDimensions.value = []
  if (oldLoading) await oldLoading.destroy().catch(() => undefined)
}

async function clearPdf(): Promise<void> {
  loadGuard.invalidate()
  renderGeneration += 1
  await resetPdfState()
}

async function renderPdf(bytes: Uint8Array): Promise<void> {
  const loadIdentity = loadGuard.begin()
  const generation = ++renderGeneration
  await resetPdfState()
  if (!mounted || !loadGuard.isCurrent(loadIdentity) || generation !== renderGeneration) return
  const task = getDocument({ data: bytes.slice() })
  loadingTask.value = task
  try {
    const loaded = await task.promise
    if (loadingTask.value !== task || generation !== renderGeneration) {
      await task.destroy()
      return
    }
    pdf.value = loaded
    pageCount.value = loaded.numPages
    pageStart.value = 1
    await nextTick()
    await startVisibleRender(loaded)
  } catch (cause) {
    if (loadingTask.value === task && !(cause instanceof Error && cause.name === 'RenderingCancelledException')) {
      previewError.value = 'PDF 原件预览失败，请重新上传。'
    }
  }
}

async function startVisibleRender(loaded: PDFDocumentProxy): Promise<void> {
  renderGeneration += 1
  await renderVisiblePages(loaded, renderGeneration)
}

async function renderVisiblePages(
  loaded: PDFDocumentProxy,
  generation: number,
): Promise<void> {
  for (const task of renderTasks.values()) task.cancel()
  renderTasks.clear()
  await nextTick()
  if (generation !== renderGeneration || loaded !== pdf.value) return
  try {
    const nextScales: number[] = []
    const nextTrusted: boolean[] = []
    for (const pageNumber of visiblePages.value) {
      const page = await loaded.getPage(pageNumber)
      if (generation !== renderGeneration || loaded !== pdf.value) return
      const base = page.getViewport({ scale: 1 })
      const expected = props.run?.page_dimensions[pageNumber - 1]
      nextTrusted[pageNumber - 1] = Boolean(
        validDimension(expected)
        && Math.abs(base.width - expected[0]) <= 1
        && Math.abs(base.height - expected[1]) <= 1,
      )
      const scale = boundedCanvasScale(base.width, base.height)
      const viewport = page.getViewport({ scale })
      nextScales[pageNumber - 1] = scale
      const canvas = canvases.get(pageNumber)
      if (!canvas) continue
      canvas.width = Math.max(1, Math.floor(viewport.width))
      canvas.height = Math.max(1, Math.floor(viewport.height))
      const context = canvas.getContext('2d')
      if (!context) throw new Error('canvas_unavailable')
      const renderTask = page.render({ canvas, canvasContext: context, viewport })
      renderTasks.set(pageNumber, renderTask)
      await renderTask.promise
      renderTasks.delete(pageNumber)
      if (generation !== renderGeneration || loaded !== pdf.value) return
    }
    scales.value = Object.assign([...scales.value], nextScales)
    trustedDimensions.value = Object.assign([...trustedDimensions.value], nextTrusted)
    if (props.run && (
      props.run.page_dimensions.length !== loaded.numPages
      || nextTrusted.some((trusted) => !trusted)
    )) {
      previewError.value = 'PDF 页尺寸与审查锚点不一致，已隐藏批注并保留原件预览。'
    }
  } catch (cause) {
    if (
      mounted
      && generation === renderGeneration
      && loaded === pdf.value
      && !(cause instanceof Error && cause.name === 'RenderingCancelledException')
    ) {
      previewError.value = 'PDF 原件预览失败，请重新上传。'
    }
  }
}

function changePage(start: number): void {
  pageStart.value = Math.max(1, Math.min(start, Math.max(1, pageCount.value - PAGE_WINDOW + 1)))
  selected.value = null
  if (pdf.value) void startVisibleRender(pdf.value)
}

watch(
  () => props.pdfBytes,
  (bytes) => {
    if (bytes) void renderPdf(bytes)
    else void clearPdf()
  },
  { immediate: true },
)

watch(
  () => props.run,
  () => {
    if (pdf.value) void startVisibleRender(pdf.value)
  },
)

onBeforeUnmount(() => {
  mounted = false
  void clearPdf()
})
</script>

<template>
  <section class="pdf-review" aria-label="合同原件与批注">
    <p v-if="previewError" class="preview-error" role="alert">{{ previewError }}</p>
    <p v-else-if="pdfBytes && pageCount === 0" class="preview-state">正在加载 PDF 原件…</p>
    <nav v-if="pageCount > PAGE_WINDOW" class="page-nav" aria-label="PDF 分页">
      <button type="button" :disabled="pageStart === 1" @click="changePage(pageStart - PAGE_WINDOW)">上一组</button>
      <span>第 {{ pageStart }}–{{ Math.min(pageCount, pageStart + PAGE_WINDOW - 1) }} 页，共 {{ pageCount }} 页</span>
      <button type="button" :disabled="pageStart + PAGE_WINDOW > pageCount" @click="changePage(pageStart + PAGE_WINDOW)">下一组</button>
    </nav>
    <div v-for="page in visiblePages" :key="page" class="page-wrap">
      <canvas :ref="(element) => canvasRef(page, element)" :aria-label="`合同第 ${page} 页`" />
      <button
        v-for="overlay in pageOverlays(page)"
        :key="`${overlay.issue.block_id}:${overlay.anchorId}:${overlay.issue.start}`"
        class="issue-mark"
        :class="`severity-${overlay.issue.severity}`"
        :style="overlayStyle(overlay)"
        type="button"
        aria-label="查看修改建议"
        @click="selected = overlay.issue"
      />
    </div>

    <aside v-if="selected" class="issue-popover" role="dialog" aria-label="合同批注详情">
      <button class="close" type="button" aria-label="关闭批注" @click="selected = null">×</button>
      <h3>修改建议</h3>
      <p class="suggestion">{{ selected.suggestion }}</p>
    </aside>
  </section>
</template>

<style scoped>
.pdf-review { position: relative; display: grid; gap: 1rem; justify-items: center; }
.page-nav { position: sticky; top: 4.1rem; z-index: 4; display: flex; align-items: center; gap: 0.8rem; padding: 0.5rem 0.7rem; border: 1px solid var(--color-border); border-radius: 8px; background: rgb(255 255 255 / 94%); box-shadow: var(--shadow-card); }
.page-nav span { color: var(--color-text-secondary); font-size: 0.82rem; }
.page-wrap { position: relative; max-width: 100%; overflow: hidden; background: white; box-shadow: 0 2px 10px rgb(20 27 40 / 12%); }
canvas { display: block; max-width: 100%; height: auto; }
.issue-mark { position: absolute; padding: 0; border: 1px solid rgb(190 36 36 / 80%); border-radius: 2px; background: rgb(230 54 54 / 24%); cursor: pointer; }
.issue-mark:hover, .issue-mark:focus-visible { background: rgb(230 54 54 / 40%); outline: 2px solid #b42323; }
.severity-high { border-width: 2px; }
.issue-popover { position: sticky; bottom: 1rem; z-index: 5; width: min(34rem, calc(100vw - 3rem)); padding: 1rem 1.1rem; border: 1px solid var(--color-danger-border); border-radius: 10px; background: white; box-shadow: 0 8px 30px rgb(20 27 40 / 22%); justify-self: end; }
.issue-popover h3 { margin: 0.15rem 2rem 0.7rem 0; font-size: 1rem; }
.suggestion { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
.close { position: absolute; top: 0.55rem; right: 0.65rem; padding: 0.05rem 0.45rem; border: 0; background: transparent; color: var(--color-text-muted); font-size: 1.4rem; }
.preview-state { color: var(--color-text-muted); font-size: 0.82rem; }
.preview-error { color: var(--color-danger-text); }
</style>
