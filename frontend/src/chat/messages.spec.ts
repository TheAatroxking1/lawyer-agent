import { describe, expect, it } from 'vitest'

import { composeChatMessages } from './messages'

const turn = (role: 'user' | 'assistant', content: string) => ({ role, content })

describe('composeChatMessages', () => {
  it('maps turns to backend message shapes in order', () => {
    const messages = composeChatMessages([
      turn('user', '第一条'),
      turn('assistant', '回答一'),
      turn('user', '第二条'),
    ])
    expect(messages).toEqual([
      { role: 'user', content: '第一条' },
      { role: 'assistant', content: '回答一' },
      { role: 'user', content: '第二条' },
    ])
  })

  it('keeps only the most recent turns when history grows', () => {
    const history = Array.from({ length: 20 }, (_, i) =>
      turn(i % 2 === 0 ? 'user' : 'assistant', `消息 ${i}`),
    )
    const messages = composeChatMessages(history, 6)
    expect(messages).toHaveLength(6)
    expect(messages[0]).toEqual({ role: 'user', content: '消息 14' })
    expect(messages[5]).toEqual({ role: 'assistant', content: '消息 19' })
  })

  it('drops blank turns but never rewrites content', () => {
    const messages = composeChatMessages([
      turn('user', '   '),
      turn('assistant', '有效回答'),
      turn('user', ''),
    ])
    expect(messages).toEqual([{ role: 'assistant', content: '有效回答' }])
  })

  it('handles an empty conversation', () => {
    expect(composeChatMessages([])).toEqual([])
  })
})
