// Small presentational helpers (pure, unit-testable).

const VERSION_STATUS_LABELS: Record<string, string> = {
  current: '现行',
  repealed: '已废止',
  historical: '历史版本',
  status_unknown: '状态未知',
}

const LEGAL_CATEGORY_LABELS: Record<string, string> = {
  constitution: '宪法',
  law: '法律',
  administrative_regulation: '行政法规',
  judicial_interpretation: '司法解释',
  local_regulation: '地方法规',
  supervisory_regulation: '监察法规',
  unknown: '类别未知',
}

export function legalCategoryLabel(category: string | null | undefined): string {
  if (!category) return LEGAL_CATEGORY_LABELS.unknown
  return LEGAL_CATEGORY_LABELS[category] ?? category
}

export function versionStatusLabel(status: string): string {
  return VERSION_STATUS_LABELS[status] ?? status
}

export function isoDate(value: string | null | undefined): string {
  if (!value) return ''
  const day = value.slice(0, 10)
  return /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : ''
}

export function formatText(value: string | null | undefined): string {
  return value ?? ''
}
