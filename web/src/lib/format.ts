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

/** 距某天还有几天，负数表示已经过去。算不出返回 null。 */
export function daysUntil(iso: string, today: Date = new Date()): number | null {
  const ymd = parseIso(iso)
  if (!ymd) return null
  // 两边都归到 UTC 午夜再相减：直接减 Date 对象会带上「现在几点」，
  // 于是同一天里早晚跑出来的天数差一天。
  const due = Date.UTC(ymd.year, ymd.month - 1, ymd.day)
  const now = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate())
  return Math.round((due - now) / 86_400_000)
}

/**
 * 复验到期的标注文案；还没到期（或算不出）时返回 null。
 *
 * 设计 4.6：到期后「在界面上标注而不隐藏」——过期的经验仍然展示，
 * 只是必须让用户知道自己正在看旧信息。所以这里只产出文案，
 * **不决定显示与否**：调用方照样渲染那条结论，多挂一句话而已。
 *
 * 判据与后端 `lushu/services/verify.py` 一致：**到期当天就算到期**。
 * 这里刻意没有「快到期」这一档：那需要提前多少天的阈值，而阈值只能在
 * 一个地方定义（后端的 `SOON_DAYS`）。前端再抄一份 14，两处迟早会分岔，
 * 分岔的表现是界面上说新鲜、命令行说已经过期。
 */
export function describeDue(iso: string | null, today: Date = new Date()): string | null {
  if (!iso) return null
  const left = daysUntil(iso, today)
  if (left === null || left > 0) return null
  if (left === 0) return '复验今天到期'
  return `复验已到期 ${-left} 天`
}
