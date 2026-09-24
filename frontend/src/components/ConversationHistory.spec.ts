// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ chats: vi.fn(), reviews: vi.fn() }))
vi.mock('../api/conversations', () => ({
  listConversations: mocks.chats, listReviewHistory: mocks.reviews,
}))
import ConversationHistory from './ConversationHistory.vue'

function render() {
  return mount(ConversationHistory, {
    props: { tenantId: 'tenant-a', identity: 'account-a' },
    global: { stubs: { RouterLink: { props: ['to'], template: '<a :data-to="JSON.stringify(to)"><slot /></a>' } } },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.chats.mockResolvedValue({ items: [{ id: 'chat-1', title: '租房咨询', updated_at: '2026-09-21T02:00:00Z' }], next_cursor: null })
  mocks.reviews.mockResolvedValue({ items: [{ id: 'review-1', file_name: '公寓协议.pdf', status: 'draft', updated_at: '2026-09-21T03:00:00Z' }], next_cursor: null })
})

describe('真实会话侧栏', () => {
  it('列出数据库会话与合同记录，链接指向可恢复的历史ID', async () => {
    const wrapper = render()
    await flushPromises()
    expect(wrapper.text()).toContain('租房咨询')
    expect(wrapper.text()).toContain('公寓协议.pdf')
    const links = wrapper.findAll('a')
    expect(links[0]!.attributes('data-to')).toContain('review-1')
    expect(links[1]!.attributes('data-to')).toContain('chat-1')
    expect(links[1]!.attributes('data-to')).toContain('tenant-a')
    wrapper.unmount()
  })

  it('合同后续对话合并成一条历史并同时恢复合同与消息', async () => {
    mocks.chats.mockResolvedValueOnce({
      items: [{ id: 'chat-1', title: '继续讨论公寓协议', updated_at: '2026-09-21T04:00:00Z', review_id: 'review-1' }],
      next_cursor: null,
    })
    const wrapper = render()
    await flushPromises()

    const links = wrapper.findAll('a')
    expect(links).toHaveLength(1)
    expect(links[0]!.text()).toContain('继续讨论公寓协议')
    expect(links[0]!.text()).toContain('合同对话')
    expect(links[0]!.attributes('data-to')).toContain('chat-1')
    expect(links[0]!.attributes('data-to')).toContain('review-1')
    wrapper.unmount()
  })

  it('切账号立即清空旧历史，拒绝晚到结果写回', async () => {
    let complete!: (value: unknown) => void
    mocks.chats.mockReturnValueOnce(new Promise(resolve => { complete = resolve }))
    const wrapper = render()
    await wrapper.setProps({ identity: '', tenantId: '' })
    complete({ items: [{ id: 'private', title: '旧账号私密内容', updated_at: '2026-09-21' }], next_cursor: null })
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧账号私密内容')
    expect(wrapper.findAll('a')).toHaveLength(0)
    wrapper.unmount()
  })

  it('读取失败可重试，不能伪装成没有历史', async () => {
    mocks.chats.mockRejectedValueOnce(new Error('offline'))
    const wrapper = render()
    await flushPromises()
    expect(wrapper.text()).toContain('历史记录加载失败')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('租房咨询')
    wrapper.unmount()
  })
})
