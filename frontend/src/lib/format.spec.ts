import { describe, expect, it } from 'vitest'

import { isoDate, legalCategoryLabel, versionStatusLabel } from './format'

describe('legalCategoryLabel', () => {
  it.each([
    ['constitution', '宪法'],
    ['law', '法律'],
    ['administrative_regulation', '行政法规'],
    ['judicial_interpretation', '司法解释'],
    ['local_regulation', '地方法规'],
    ['supervisory_regulation', '监察法规'],
    ['unknown', '类别未知'],
  ])('maps %s to %s', (category, label) => {
    expect(legalCategoryLabel(category)).toBe(label)
  })

  it('uses the unknown label for an absent category', () => {
    expect(legalCategoryLabel(null)).toBe('类别未知')
    expect(legalCategoryLabel(undefined)).toBe('类别未知')
    expect(legalCategoryLabel('')).toBe('类别未知')
  })

  it('keeps an unrecognized category visible', () => {
    expect(legalCategoryLabel('departmental_rule')).toBe('departmental_rule')
  })
})

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
