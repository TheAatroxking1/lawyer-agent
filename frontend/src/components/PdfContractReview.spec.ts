// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ContractReviewRun } from '../api/contractReview'

const pdf = vi.hoisted(() => ({
  getDocument: vi.fn(),
  destroy: vi.fn(),
}))

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: pdf.getDocument,
}))

import PdfContractReview from './PdfContractReview.vue'

describe('PDF contract highlights', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    pdf.destroy.mockResolvedValue(undefined)
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as CanvasRenderingContext2D)
    pdf.getDocument.mockReturnValue({
      promise: Promise.resolve({
        numPages: 1,
        getPage: async () => ({
          getViewport: ({ scale }: { scale: number }) => ({ width: 200 * scale, height: 200 * scale }),
          render: () => ({ promise: Promise.resolve(), cancel: vi.fn() }),
        }),
      }),
      destroy: pdf.destroy,
    })
  })

  it('renders every line highlight and opens one suggestion-only bubble from any marker', async () => {
    const run = {
      id: 'run-1', status: 'draft', file_name: '合同.pdf', page_dimensions: [[200, 200]],
      blocks: [
        { block_id: 'b1', text: '甲方可', anchors: [{ anchor_id: 'a1', page: 1, start: 0, end: 3, quad: [0, 20, 80, 20, 80, 30, 0, 30] }] },
        { block_id: 'b2', text: '单方修改', anchors: [{ anchor_id: 'a2', page: 1, start: 0, end: 4, quad: [0, 40, 100, 40, 100, 50, 0, 50] }] },
        { block_id: 'b3', text: '服务规则', anchors: [{ anchor_id: 'a3', page: 1, start: 0, end: 4, quad: [0, 60, 100, 60, 100, 70, 0, 70] }] },
      ],
      issues: [{
        block_id: 'b1', anchor_id: 'a1', start: 0, end: 3,
        quote: '甲方可\n单方修改\n服务规则', category: 'legal', severity: 'medium',
        problem: '甲方拥有单方修改权。', suggestion: '重大变更必须经双方书面确认。',
        evidence_ids: ['law-1'],
        evidence_passages: [{ passage_id: 'law1.p1', document_id: 'law-1', quote: '法律条文不应出现在气泡中' }],
        highlights: [
          { block_id: 'b1', anchor_id: 'a1', start: 0, end: 3 },
          { block_id: 'b2', anchor_id: 'a2', start: 0, end: 4 },
          { block_id: 'b3', anchor_id: 'a3', start: 0, end: 4 },
        ],
      }],
    } as unknown as ContractReviewRun
    const wrapper = mount(PdfContractReview, {
      props: { pdfBytes: new Uint8Array([37, 80, 68, 70]), run },
    })
    await flushPromises()

    const marks = wrapper.findAll('.issue-mark')
    expect(marks).toHaveLength(3)
    await marks[1].trigger('click')

    const bubble = wrapper.get('[aria-label="合同批注详情"]')
    expect(bubble.text()).toContain('修改建议')
    expect(bubble.text()).toContain('重大变更必须经双方书面确认。')
    expect(bubble.text()).not.toContain('甲方拥有单方修改权。')
    expect(bubble.text()).not.toContain('法律条文不应出现在气泡中')
    expect(bubble.text()).not.toContain('甲方可\n单方修改\n服务规则')
    wrapper.unmount()
  })
})
