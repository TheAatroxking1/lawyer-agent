import { describe, expect, it } from 'vitest'

import { isoDate, versionStatusLabel } from './format'

describe('versionStatusLabel', () => {
  it('maps known statuses to Chinese labels', () => {
    expect(versionStatusLabel('current')).toBe('现行')
    expect(versionStatusLabel('repealed')).toBe('已废止')
    expect(versionStatusLabel('historical')).toBe('历史版本')
    expect(versionStatusLabel('status_unknown')).toBe('状态未知')
  })

  it('falls back to the raw value for unknown statuses', () => {
    expect(versionStatusLabel('draft')).toBe('draft')
  })
})

describe('isoDate', () => {
  it('returns the ISO date portion for full timestamps', () => {
    expect(isoDate('2024-05-01T00:00:00Z')).toBe('2024-05-01')
  })

  it('returns an empty string for null, empty or malformed values', () => {
    expect(isoDate(null)).toBe('')
    expect(isoDate(undefined)).toBe('')
    expect(isoDate('')).toBe('')
    expect(isoDate('not-a-date')).toBe('')
  })
})
