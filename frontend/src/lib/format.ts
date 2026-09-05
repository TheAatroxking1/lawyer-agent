// Small presentational helpers (pure, unit-testable).

const VERSION_STATUS_LABELS: Record<string, string> = {
  current: '现行',
  repealed: '已废止',
  historical: '历史版本',
  status_unknown: '状态未知',
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
