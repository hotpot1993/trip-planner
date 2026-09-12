/**
 * 日期与文本格式化。
 *
 * 刻意不用 `new Date('2026-10-01')`：那会按 UTC 午夜解析，在西半球时区下
 * `getDay()` 会退回前一天，星期就错了。这里一律用 Date.UTC 自己算。
 */

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'] as const

interface Ymd {
  year: number
  month: number
  day: number
}

export function parseIso(iso: string): Ymd | null {
  const parts = iso.split('-')
  if (parts.length !== 3) return null

  const [y, m, d] = parts.map(Number)
  if (y === undefined || m === undefined || d === undefined) return null
  if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) return null
  return { year: y, month: m, day: d }
}

/** 星期几，如「周四」。 */
export function weekday(iso: string): string {
  const ymd = parseIso(iso)
  if (!ymd) return ''
  const utc = new Date(Date.UTC(ymd.year, ymd.month - 1, ymd.day))
  return `周${WEEKDAYS[utc.getUTCDay()] ?? ''}`
}

/** 月.日，如「10.01」。 */
export function monthDay(iso: string): string {
  const ymd = parseIso(iso)
  if (!ymd) return iso
  return `${String(ymd.month).padStart(2, '0')}.${String(ymd.day).padStart(2, '0')}`
}

/** 完整日期，如「2026.10.01」。 */
export function fullDate(iso: string): string {
  const ymd = parseIso(iso)
  if (!ymd) return iso
  return `${ymd.year}.${monthDay(iso)}`
}

/** 日期区间，如「2026.10.01 – 10.03」。同年同月时省略重复部分。 */
export function dateRange(startIso: string, endIso: string): string {
  const start = parseIso(startIso)
  const end = parseIso(endIso)
  if (!start || !end) return `${startIso} – ${endIso}`

  if (start.year === end.year && start.month === end.month) {
    return `${fullDate(startIso)} – ${String(end.day).padStart(2, '0')}`
  }
  if (start.year === end.year) {
    return `${fullDate(startIso)} – ${monthDay(endIso)}`
  }
  return `${fullDate(startIso)} – ${fullDate(endIso)}`
}

/** 天项的类型标签。 */
export function kindLabel(kind: string): string {
  switch (kind) {
    case 'poi':
      return '景点'
    case 'meal':
      return '用餐'
    case 'rest':
      return '休息'
    default:
      return kind
  }
}

/** 后端的时间戳（带时区偏移的 ISO）转成可读的本地时间。 */
export function shortStamp(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${parsed.getFullYear()}.${pad(parsed.getMonth() + 1)}.${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
}
