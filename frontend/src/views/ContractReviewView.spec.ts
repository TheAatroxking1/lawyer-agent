// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'

const mocks = vi.hoisted(() => ({
  tenants: vi.fn(), switch: vi.fn(), run: vi.fn(), runDemo: vi.fn(), chat: vi.fn(), preview: vi.fn(),
  token: 'account', tenantToken: '',
  createChat: vi.fn(), getChat: vi.fn(), getReview: vi.fn(), getPdf: vi.fn(),
  query: {} as Record<string, string>, replace: vi.fn(), activeTenant: null as unknown,
}))
vi.mock('../api', async (original) => ({
  ...await original<typeof import('../api')>(),
  myTenants: mocks.tenants, switchTenant: mocks.switch,
}))
vi.mock('../api/contractReview', async (original) => ({
  ...await original<typeof import('../api/contractReview')>(),
  runContractReview: mocks.run, runDemoReview: mocks.runDemo, readPdfPreview: mocks.preview,
  createContractReviewApi: () => ({ get: mocks.getReview, document: mocks.getPdf }),
}))
vi.mock('../api/sse', () => ({ chatStream: mocks.chat }))
vi.mock('../api/conversations', () => ({ createConversation: mocks.createChat, getConversation: mocks.getChat, sendConversationMessage: mocks.chat }))
vi.mock('vue-router', () => ({ useRoute: () => ({ query: mocks.query }), useRouter: () => ({ replace: mocks.replace }) }))
vi.mock('../auth/transport', () => ({
  ensureSession: vi.fn(),
  selectTenant: async (tenant: unknown) => { await mocks.switch(tenant); mocks.activeTenant = tenant; mocks.tenantToken = 'tenant' },
}))
vi.mock('../auth/session', () => ({ session: {
  readToken: () => mocks.token, readTenantToken: () => mocks.tenantToken,
  clearTenantToken: () => { mocks.tenantToken = '' },
  saveTenantToken: (token: string) => { mocks.tenantToken = token },
  readActiveTenant: () => mocks.activeTenant, clearActiveTenant: () => { mocks.activeTenant = null },
  accountIdentity: () => mocks.token, tenantIdentity: () => mocks.tenantToken,
} }))
vi.mock('../auth/state', () => ({ authState: reactive({ authenticated: true }), refreshAuth: vi.fn() }))
import ContractReviewView from './ContractReviewView.vue'
import { authState } from '../auth/state'

const tenant = { tenant_id: 't1', membership_id: 'm1', name: '测试租户', membership_status: 'active', tenant_status: 'active' }
function render() {
  return mount(ContractReviewView, { global: { stubs: { PdfContractReview: true, AuthModal: true } } })
}
beforeEach(() => {
  vi.clearAllMocks()
  mocks.token = 'account'
  authState.authenticated = true
  mocks.tenantToken = ''
  mocks.activeTenant = null
  mocks.query = {}
  mocks.replace.mockResolvedValue(undefined)
  mocks.createChat.mockResolvedValue({ id: 'chat-new', title: '新对话' })
  mocks.getChat.mockResolvedValue({ id: 'chat-old', title: '旧对话', messages: [{ id: 'm-old', role: 'assistant', content: '以前保存的回复', status: 'completed' }] })
  mocks.getReview.mockResolvedValue({ id: 'review-old', status: 'draft', file_name: '历史协议.pdf', issues: [], blocks: [], page_dimensions: [] })
  mocks.getPdf.mockResolvedValue(new Uint8Array([1]))
  mocks.tenants.mockResolvedValue({ items: [tenant] })
  mocks.switch.mockResolvedValue({ access_token: 'tenant' })
  mocks.preview.mockResolvedValue(new Uint8Array([1]))
  mocks.run.mockResolvedValue({ run: { id: 'r1', status: 'draft', file_name: '合同.pdf', issues: [], blocks: [], page_dimensions: [] }, pdf: new Uint8Array([1]) })
  mocks.runDemo.mockResolvedValue({ run: {
    id: 'demo-1', status: 'draft', file_name: '演示合同-房屋租赁.pdf', issues: [], blocks: [],
    page_dimensions: [[612, 792]], is_demo: true,
    mcp_tools: [{ name: 'document_read_blocks', capability: 'document.read_blocks', description: '读取合同原生文字', status: 'discovered' }],
  }, pdf: new Uint8Array([37, 80, 68, 70]) })
})
describe('对话工作区', () => {
  it('makes the real-contract upload the primary interview demo action', async () => {
    const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => undefined)
    const wrapper = render()
    await flushPromises()
    expect(wrapper.text()).toContain('面试演示 · 真实合同')
    expect(wrapper.text()).toContain('选择面试官的合同')
    await wrapper.get('button.interview-upload').trigger('click')
    expect(click).toHaveBeenCalled()
    click.mockRestore()
    wrapper.unmount()
  })

  it('runs the interview demo and exposes the discovered MCP catalog', async () => {
    const wrapper = render()
    await flushPromises()
    await wrapper.get('button.demo-suggestion').trigger('click')
    await flushPromises()
    expect(mocks.runDemo).toHaveBeenCalled()
    expect(wrapper.text()).toContain('演示模式')
    await wrapper.findAll('button.document-link')[1]!.trigger('click')
    expect(wrapper.text()).toContain('document_read_blocks')
    expect(wrapper.text()).toContain('读取合同原生文字')
    wrapper.unmount()
  })

  it('从历史URL恢复服务器消息，后续发送引用同一会话', async () => {
    mocks.query = { tenant: 't1', conversation: 'chat-old' }
    mocks.chat.mockResolvedValue((async function* () {
      yield { event: 'delta', data: { text: '继续回答' } }
      yield { event: 'done', data: { status: 'completed' } }
    })())
    const wrapper = render()
    await flushPromises()
    expect(wrapper.text()).toContain('以前保存的回复')
    await wrapper.get('textarea').setValue('接着说')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.chat).toHaveBeenCalledWith('t1', 'chat-old', '接着说', expect.any(AbortSignal))
    expect(mocks.createChat).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('打开已保存合同只读回PDF和结果，不再次审阅', async () => {
    mocks.query = { tenant: 't1', review: 'review-old' }
    const wrapper = render()
    await flushPromises()
    expect(wrapper.text()).toContain('历史协议.pdf')
    expect(mocks.getReview).toHaveBeenCalled()
    expect(mocks.getPdf).toHaveBeenCalled()
    expect(mocks.run).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('合同审阅后可以在同一对话继续提问并保留合同页面', async () => {
    mocks.query = { tenant: 't1', review: 'review-old' }
    mocks.chat.mockResolvedValue((async function* () {
      yield { event: 'delta', data: { text: '可以继续讨论其他问题。' } }
      yield { event: 'done', data: { status: 'completed' } }
    })())
    const wrapper = render()
    await flushPromises()
    await wrapper.get('textarea').setValue('顺便问一个其他问题')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.createChat).toHaveBeenCalledWith('t1', '顺便问一个其他问题', 'review-old')
    expect(mocks.chat).toHaveBeenCalledWith(
      't1', 'chat-new', '顺便问一个其他问题', expect.any(AbortSignal),
    )
    expect(mocks.replace).toHaveBeenCalledWith({
      path: '/chat',
      query: { tenant: 't1', conversation: 'chat-new', review: 'review-old' },
    })
    expect(wrapper.text()).toContain('历史协议.pdf')
    expect(wrapper.text()).toContain('可以继续讨论其他问题。')
    const articles = wrapper.findAll('article.message')
    const attachment = articles.findIndex(article => article.text().includes('历史协议.pdf'))
    const followup = articles.findIndex(article => article.text().includes('顺便问一个其他问题'))
    expect(attachment).toBeGreaterThanOrEqual(0)
    expect(followup).toBeGreaterThan(attachment)
    wrapper.unmount()
  })
  it('同时带合同和对话标识的历史URL恢复PDF、审阅结果和消息', async () => {
    mocks.query = { tenant: 't1', conversation: 'chat-old', review: 'review-old' }
    const wrapper = render()
    await flushPromises()
    expect(mocks.getReview).toHaveBeenCalledWith('t1', 'review-old', expect.any(AbortSignal))
    expect(mocks.getChat).toHaveBeenCalledWith('t1', 'chat-old')
    expect(wrapper.text()).toContain('历史协议.pdf')
    expect(wrapper.text()).toContain('以前保存的回复')
    expect(wrapper.text()).not.toContain('会话链接不完整')
    wrapper.unmount()
  })
  it('同一历史URL重新登录后能够再次恢复内容', async () => {
    mocks.query = { tenant: 't1', conversation: 'chat-old' }
    const wrapper = render()
    await flushPromises()
    expect(wrapper.text()).toContain('以前保存的回复')
    authState.authenticated = false
    await flushPromises()
    expect(wrapper.text()).not.toContain('以前保存的回复')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    authState.authenticated = true
    wrapper.findComponent({ name: 'AuthModal' }).vm.$emit('success')
    await flushPromises()
    expect(wrapper.text()).toContain('以前保存的回复')
    expect(mocks.getChat).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
  it('引用不合格的候选明确列为未发布，不显示为零风险', async () => {
    mocks.run.mockResolvedValue({ run: {
      id: 'r1', status: 'draft', file_name: '合同.pdf', issues: [], blocks: [], page_dimensions: [],
      withheld_issues: [{ index: 1, page: 3, reason: 'unverified_citation' }],
    }, pdf: new Uint8Array([1]) })
    const wrapper = render()
    await flushPromises()
    await wrapper.trigger('drop', { dataTransfer: { files: [new File(['x'], '合同.pdf')], types: ['Files'] } })
    await flushPromises()
    expect(wrapper.text()).toContain('1 条候选意见因引用未通过核验，未发布为批注')
    expect(wrapper.text()).toContain('不代表合同没有其他风险')
    wrapper.unmount()
  })
  it('有缺字提醒仍展示完成结果和未定位意见', async () => {
    mocks.run.mockResolvedValue({ run: {
      id: 'r1', status: 'draft', file_name: '合同.pdf', blocks: [], page_dimensions: [],
      verification_report: { pages: [{ page: 7, reasons: ['missing_text'] }] },
      evidence_documents: [{ document_id: 'law-1', title: '测试合同法' }],
      issues: [{ block_id: 'visual-7', anchor_id: null, start: 0, end: 4,
        quote: '及时付款', category: 'wording', severity: 'medium',
        problem: '付款时间不明确', suggestion: '建议明确付款期限', evidence_ids: ['law-1'],
        evidence_passages: [{ passage_id: 'law1.p1', document_id: 'law-1', quote: '检索依据原文应能直接查看' }] }],
    }, pdf: new Uint8Array([1]) })
    const wrapper = render()
    await flushPromises()
    await wrapper.trigger('drop', { dataTransfer: { files: [new File(['x'], '合同.pdf')], types: ['Files'] } })
    await flushPromises()
    expect(wrapper.text()).toContain('审阅完成')
    expect(wrapper.text()).toContain('第7页')
    expect(wrapper.text()).toContain('疑似缺字')
    expect(wrapper.text()).toContain('位置待核对')
    expect(wrapper.text()).toContain('付款时间不明确')
    expect(wrapper.text()).toContain('检索依据原文应能直接查看')
    const evidence = wrapper.get('[aria-label="引用的法律原文"]')
    expect(evidence.element.tagName).toBe('SECTION')
    expect(evidence.text()).toContain('测试合同法')
    expect(evidence.text()).toContain('法律原文（检索片段）')
    wrapper.unmount()
  })
  it('普通聊天取得独立租户令牌，保存会话且保留账号身份', async () => {
    mocks.chat.mockResolvedValue((async function* () {
      yield { event: 'delta', data: { text: '聊天已连接' } }
      yield { event: 'done', data: { status: 'completed' } }
    })())
    const wrapper = render()
    await flushPromises()
    expect(mocks.tenants).toHaveBeenCalledOnce()
    expect(mocks.switch).toHaveBeenCalledOnce()
    await wrapper.get('textarea').setValue('你好')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.chat).toHaveBeenCalledOnce()
    expect(mocks.createChat).toHaveBeenCalledWith('t1', '你好')
    expect(mocks.token).toBe('account')
    expect(wrapper.text()).toContain('聊天已连接')
    wrapper.unmount()
  })
  it('拖入页面其他区域也接收附件并阻止浏览器直接打开文件', async () => {
    const wrapper = render()
    await flushPromises()
    const event = new Event('drop', { cancelable: true })
    Object.defineProperty(event, 'dataTransfer', { value: { files: [new File(['x'], '侧栏.pdf')], types: ['Files'] } })
    window.dispatchEvent(event)
    await flushPromises()
    expect(event.defaultPrevented).toBe(true)
    expect(mocks.run).toHaveBeenCalledOnce()
    wrapper.unmount()
    const afterUnmount = new Event('drop', { cancelable: true })
    Object.defineProperty(afterUnmount, 'dataTransfer', { value: { files: [new File(['x'], 'later.pdf')], types: ['Files'] } })
    window.dispatchEvent(afterUnmount)
    expect(afterUnmount.defaultPrevented).toBe(false)
  })
  it('拖入 PDF 后默认审阅，使用租户链路而非普通聊天', async () => {
    const wrapper = render()
    await flushPromises()
    const file = new File(['%PDF-test'], '合同.pdf', { type: 'application/pdf' })
    await wrapper.trigger('drop', { dataTransfer: { files: [file], types: ['Files'] } })
    await flushPromises()
    expect(mocks.run).toHaveBeenCalledOnce()
    expect(mocks.run.mock.calls[0]?.[1]).toMatchObject({ tenantId: 't1', file, instruction: '' })
    expect(mocks.chat).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('合同.pdf')
    wrapper.unmount()
  })
  it('多个文件或非 PDF 被明确拒绝，不启动任务', async () => {
    const wrapper = render()
    await flushPromises()
    await wrapper.trigger('drop', { dataTransfer: { files: [new File(['x'], 'test.docx')], types: ['Files'] } })
    await flushPromises()
    expect(wrapper.text()).toContain('PDF')
    expect(mocks.run).not.toHaveBeenCalled()
    const pdf = new File(['x'], 'test.pdf')
    await wrapper.trigger('drop', { dataTransfer: { files: [pdf, pdf], types: ['Files'] } })
    await flushPromises()
    expect(wrapper.text()).toContain('一次')
    expect(mocks.run).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('纯文本走聊天，流未正常结束时显示未完成', async () => {
    mocks.chat.mockResolvedValue((async function* () { yield { event: 'delta', data: { text: '你好' } } })())
    const wrapper = render()
    await flushPromises()
    await wrapper.get('textarea').setValue('你好')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mocks.chat).toHaveBeenCalledOnce()
    expect(mocks.run).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('未完整')
    wrapper.unmount()
  })
  it('下一问不会把不完整回答发送给模型，原消息仍保留未完成标记', async () => {
    mocks.chat.mockResolvedValueOnce((async function* () { yield { event: 'delta', data: { text: '未写完的旧回答' } } })())
    mocks.chat.mockResolvedValueOnce((async function* () {
      yield { event: 'delta', data: { text: '新的回答' } }
      yield { event: 'done', data: { status: 'completed' } }
    })())
    const wrapper = render()
    await flushPromises()
    await wrapper.get('textarea').setValue('第一问')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.get('textarea').setValue('第二问')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(JSON.stringify(mocks.chat.mock.calls[1]?.[0])).not.toContain('未写完的旧回答')
    const oldReply = wrapper.findAll('.message.assistant')[0]!
    expect(oldReply.text()).toContain('未完成')
    expect(oldReply.text()).toContain('未写完的旧回答')
    wrapper.unmount()
  })
  it('离开页面取消正在审查的请求，晚到结果不写入', async () => {
    let resolve: (value: unknown) => void = () => {}
    mocks.run.mockReturnValue(new Promise((done) => { resolve = done }))
    const wrapper = render()
    await flushPromises()
    await wrapper.trigger('drop', { dataTransfer: { files: [new File(['x'], 'test.pdf')], types: ['Files'] } })
    await flushPromises()
    const signal = mocks.run.mock.calls[0]?.[3] as AbortSignal
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
    resolve({ run: { status: 'draft' }, pdf: new Uint8Array() })
    await flushPromises()
  })
  it('文件选择与拖放共用默认审阅入口', async () => {
    const wrapper = render()
    await flushPromises()
    const file = new File(['x'], '选择.pdf', { type: 'application/pdf' })
    Object.defineProperty(wrapper.get('input[type=file]').element, 'files', { value: [file] })
    await wrapper.get('input[type=file]').trigger('change')
    await flushPromises()
    expect(mocks.run).toHaveBeenCalledOnce()
    expect(mocks.run.mock.calls[0]?.[1].file).toBe(file)
    wrapper.unmount()
  })
  it('未登录拖入文件会保留附件，登录并取得租户会话后才开始', async () => {
    authState.authenticated = false
    mocks.token = ''
    const wrapper = render()
    await flushPromises()
    await wrapper.trigger('drop', { dataTransfer: { files: [new File(['x'], '登录后.pdf')], types: ['Files'] } })
    await flushPromises()
    expect(mocks.run).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('登录后.pdf')
    mocks.token = 'account'
    authState.authenticated = true
    wrapper.findComponent({ name: 'AuthModal' }).vm.$emit('success')
    await flushPromises()
    expect(mocks.run).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
  it('多个租户必须先明确选择，之后才上传', async () => {
    mocks.tenants.mockResolvedValue({ items: [tenant, { ...tenant, tenant_id: 't2', membership_id: 'm2', name: '第二租户' }] })
    const wrapper = render()
    await flushPromises()
    await wrapper.trigger('drop', { dataTransfer: { files: [new File(['x'], '待选择.pdf')], types: ['Files'] } })
    await flushPromises()
    expect(mocks.run).not.toHaveBeenCalled()
    await wrapper.findAll('button').find(button => button.text() === '第二租户')!.trigger('click')
    await flushPromises()
    expect(mocks.run.mock.calls[0]?.[1].tenantId).toBe('t2')
    wrapper.unmount()
  })
  it('停止后忽略晚到结果，清理对话不会残留 PDF', async () => {
    let resolve: (value: unknown) => void = () => {}
    mocks.run.mockReturnValue(new Promise(done => { resolve = done }))
    const wrapper = render()
    await flushPromises()
    await wrapper.trigger('drop', { dataTransfer: { files: [new File(['x'], '旧合同.pdf')], types: ['Files'] } })
    await flushPromises()
    const signal = mocks.run.mock.calls[0]?.[3] as AbortSignal
    await wrapper.get('[aria-label="停止生成"]').trigger('click')
    expect(signal.aborted).toBe(true)
    await wrapper.findAll('button').find(button => button.text() === '新对话')!.trigger('click')
    resolve({ run: { status: 'draft', file_name: '旧合同.pdf' }, pdf: new Uint8Array([1]) })
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧合同.pdf')
    expect(wrapper.find('[aria-label="PDF 原文预览"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
