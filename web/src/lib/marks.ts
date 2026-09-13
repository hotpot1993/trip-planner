/**
 * 正文的标记分层。
 *
 * 工作台的正文上同时有**两层**标记：人工标注的结论，与模型抽出的候选。
 * 两层都会重叠（同一句话可以既是避坑又是「入口」类），而 HTML 不能嵌套
 * 任意交叉的区间。所以先把所有区间切成**互不重叠的原子段**，
 * 每一段记下它被哪些层覆盖，渲染时再合并样式。
 *
 * 这里刻意保持纯函数：它是整页最容易出错的地方（偏移错一个字，
 * 高亮的就不是那句话），而纯函数能在没有浏览器的情况下逐条验算。
 */

export interface MarkSpan {
  id: string
  start: number
  end: number
  /** human = 人工标注；machine = 模型候选。两者的视觉权重必须拉开。 */
  layer: 'human' | 'machine'
  polarity: 'avoid' | 'highlight'
}

export interface Segment {
  start: number
  end: number
  text: string
  /** 覆盖这一段的所有标记，顺序与传入顺序一致。 */
  spans: MarkSpan[]
}

/**
 * 把正文按标记边界切成原子段。
 *
 * 越界的区间会被夹到 `[0, body.length]`，而不是被丢掉：库里存着的是当初
 * 算出来的偏移，素材正文若被改过就会越界，丢掉它等于让标注在界面上凭空消失
 * （人只会以为自己的标注没了）。
 */
export function segmentBody(body: string, spans: MarkSpan[]): Segment[] {
  const total = body.length
  const clipped = spans
    .map((span) => ({
      ...span,
      start: clamp(span.start, 0, total),
      end: clamp(span.end, 0, total),
    }))
    .filter((span) => span.end > span.start)

  const points = new Set<number>([0, total])
  for (const span of clipped) {
    points.add(span.start)
    points.add(span.end)
  }

  const ordered = [...points].sort((a, b) => a - b)
  const segments: Segment[] = []
  for (let i = 0; i < ordered.length - 1; i += 1) {
    const start = ordered[i]!
    const end = ordered[i + 1]!
    if (end <= start) continue
    segments.push({
      start,
      end,
      text: body.slice(start, end),
      spans: clipped.filter((span) => span.start <= start && span.end >= end),
    })
  }
  return segments
}

function clamp(value: number, low: number, high: number): number {
  if (Number.isNaN(value)) return low
  return Math.min(Math.max(value, low), high)
}

/** 一段文字上都有哪些层。渲染时据此决定样式，也用于测试。 */
export function layersOf(segment: Segment): { human: MarkSpan | null; machine: boolean } {
  const human = segment.spans.find((span) => span.layer === 'human') ?? null
  return { human, machine: segment.spans.some((span) => span.layer === 'machine') }
}

/**
 * 从 DOM 选区反推正文里的字符偏移。
 *
 * 靠每段渲染时写下的 `data-start`：段内是单个文本节点，所以
 * `data-start + 段内偏移` 就是正文偏移。这比给整篇正文套一个文本节点可靠——
 * 有标记的时候正文必然被切成多段。
 *
 * 两处必须挡住的坑：
 * - 起点/终点落在元素上（而不是文本节点）时，`offset` 是**子节点序号**不是
 *   字符偏移，直接拿来算会得到一个看似合理的错数字；
 * - 选中跨越标记边界时两侧文本理应逐字相同，不同就说明偏移算错了。
 *
 * 反向选择（从后往前拖）由浏览器自己归一，这里不做处理。
 */
export function selectionRange(
  selection: Selection | null,
  body: string,
): { start: number; end: number; text: string } | null {
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return null
  const range = selection.getRangeAt(0)
  const raw = range.toString()
  if (!raw.trim()) return null

  const start = absoluteOffset(range.startContainer, range.startOffset)
  const end = absoluteOffset(range.endContainer, range.endOffset)
  if (start === null || end === null || end <= start) return null

  const text = body.slice(start, end)
  if (text !== raw) return null
  return { start, end, text }
}

function absoluteOffset(node: Node, offset: number): number | null {
  if (node.nodeType !== Node.TEXT_NODE) return null
  const base = closestStart(node)
  return base === null ? null : base + offset
}

function closestStart(node: Node): number | null {
  const element = node instanceof Element ? node : (node.parentElement ?? null)
  const holder = element?.closest<HTMLElement>('[data-start]')
  if (!holder) return null
  const base = Number(holder.dataset.start ?? Number.NaN)
  return Number.isNaN(base) ? null : base
}
