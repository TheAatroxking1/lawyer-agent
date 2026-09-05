import { describe, expect, it } from 'vitest'

import { parseSseBlock, parseSseStream } from './sse'

describe('parseSseBlock', () => {
  it('parses event and single data lines', () => {
    const frame = parseSseBlock('event: started\ndata: {"question":"q"}')
    expect(frame.event).toBe('started')
    expect(frame.data).toBe('{"question":"q"}')
  })

  it('joins multiple data lines with a newline', () => {
    const frame = parseSseBlock('event: answer\ndata: {"a":1}\ndata: tail')
    expect(frame.data).toBe('{"a":1}\ntail')
  })

  it('ignores comments and unknown lines', () => {
    const frame = parseSseBlock(': keep\nevent: done\ndata: {}')
    expect(frame.event).toBe('done')
    expect(frame.data).toBe('{}')
  })

  it('returns an empty frame for an empty block', () => {
    expect(parseSseBlock('')).toEqual({})
  })
})

describe('parseSseStream', () => {
  it('splits frames on blank lines', () => {
    const text = [
      'event: started',
      'data: {"q":1}',
      '',
      'event: answer',
      'data: {"text":"hi"}',
      '',
      '',
      'event: done',
      'data: {}',
      '',
    ].join('\n')
    const frames = parseSseStream(text)
    expect(frames.map((frame) => frame.event)).toEqual(['started', 'answer', 'done'])
    expect(frames[1].data).toBe('{"text":"hi"}')
  })

  it('handles carriage-return line endings', () => {
    const text = 'event: done\r\ndata: {}\r\n\r\n'
    expect(parseSseStream(text)).toEqual([{ event: 'done', data: '{}' }])
  })

  it('returns no frames for whitespace-only input', () => {
    expect(parseSseStream('   \n\n')).toEqual([])
  })
})
