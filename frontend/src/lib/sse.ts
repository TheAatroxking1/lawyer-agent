// Minimal SSE wire parsing (framework-free, unit-testable).
//
// The backend streams frames as:
//   event: <name>\ndata: <json>\n\n
// A frame may carry several data: lines which the spec joins with \n.

export interface SseFrame {
  event?: string
  data?: string
}

export function parseSseBlock(block: string): SseFrame {
  const frame: SseFrame = {}
  const dataLines: string[] = []
  for (const raw of block.split(/\r?\n/)) {
    const line = raw.trimEnd()
    if (line.startsWith('event:')) {
      frame.event = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trim())
    }
  }
  if (dataLines.length > 0) {
    frame.data = dataLines.join('\n')
  }
  return frame
}

export function parseSseStream(text: string): SseFrame[] {
  const frames: SseFrame[] = []
  for (const block of text.split(/\n\n+/)) {
    const trimmed = block.trim()
    if (trimmed.length === 0) continue
    frames.push(parseSseBlock(trimmed))
  }
  return frames
}
