import type { ReactNode } from 'react'

import type { DayOut, ItemOut, StayOut } from '@/lib/api'
import { kindLabel, monthDay, weekday } from '@/lib/format'

/**
 * 驿道线：行程的签名元素。
 *
 * 一条连续的竖线从第一站贯穿到最后一天，站点与天都是挂在线上的节点。
 * 它不只是装饰——线本身编码了信息：
 *
 * - 圆点的大小区分「城市停留」与「一天」
 * - 圆点的实心 / 空心区分「已安排」与「未安排」
 * - 城际转移在 M2 会作为第三种节点插进这条线，且那一段线改为虚线
 *
 * 把城市与天放进同一个扁平序列，是为了让这条线真的连续。嵌套列表在视觉上
 * 会断成几截，那样它就退化成普通的日程列表了。
 */

type RailNode =
  | { kind: 'station'; key: string; stay: StayOut; index: number }
  | { kind: 'day'; key: string; stay: StayOut; day: DayOut }

const CN_NUMERALS = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'] as const

function stationLabel(index: number): string {
  return `第${CN_NUMERALS[index] ?? String(index + 1)}站`
}

function buildNodes(stays: StayOut[]): RailNode[] {
  const nodes: RailNode[] = []
  stays.forEach((stay, index) => {
    nodes.push({ kind: 'station', key: `station-${index}`, stay, index })
    stay.days.forEach((day) => {
      nodes.push({ kind: 'day', key: `day-${index}-${day.date}`, stay, day })
    })
  })
  return nodes
}

export function RouteRail({ stays }: { stays: StayOut[] }) {
  const nodes = buildNodes(stays)

  if (nodes.length === 0) {
    return (
      <p className="rounded border border-dashed border-rule px-4 py-6 text-sm text-ink-3">
        这份行程还没有城市。先在下方添加一座，或者用规划生成。
      </p>
    )
  }

  const hasAnyItem = nodes.some(
    (node) => node.kind === 'day' && node.day.items.length > 0,
  )

  return (
    <div>
      {/* 整份都空的时候只说一次。逐天重复同一句话是噪声，不是引导。 */}
      {hasAnyItem ? null : (
        <p className="mb-5 rounded border border-dashed border-rule px-4 py-3 text-sm text-ink-3">
          整份行程还没有内容。用规划生成后，每一天都会自动排满。
        </p>
      )}
      <ol className="m-0 list-none p-0">
        {nodes.map((node, index) => (
          <RailRow key={node.key} node={node} isLast={index === nodes.length - 1} />
        ))}
      </ol>
    </div>
  )
}

function RailRow({ node, isLast }: { node: RailNode; isLast: boolean }) {
  return (
    <li className="grid grid-cols-[26px_1fr]">
      <RailGutter line={!isLast} marker={node.kind === 'station' ? <StationDot /> : <DayTick filled={node.day.items.length > 0} />} />
      {node.kind === 'station' ? <StationBody node={node} /> : <DayBody day={node.day} />}
    </li>
  )
}

function RailGutter({ line, marker }: { line: boolean; marker: ReactNode }) {
  return (
    <div className="relative flex justify-center">
      {/* 从本节点的圆点中心往下画，下一个节点的圆点会盖住接缝，于是线是连续的 */}
      {line && <span aria-hidden className="absolute top-[14px] bottom-0 w-px bg-rule" />}
      <span className="relative pt-[8px]">{marker}</span>
    </div>
  )
}

/** 城市停留的圆点：大、实心、石青。 */
function StationDot() {
  return (
    <span
      aria-hidden
      className="block size-3 rounded-full bg-azurite ring-4 ring-paper"
    />
  )
}

/** 一天的刻度：已安排为实心石绿，未安排为空心。 */
function DayTick({ filled }: { filled: boolean }) {
  return (
    <span
      aria-hidden
      className={
        filled
          ? 'block size-3 rounded-full bg-malachite ring-4 ring-paper'
          : 'block size-3 rounded-full border-2 border-rule bg-paper ring-4 ring-paper'
      }
    />
  )
}

function StationBody({
  node,
}: {
  node: Extract<RailNode, { kind: 'station' }>
}) {
  const { stay, index } = node
  return (
    <div className="pb-2">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-display text-lg leading-tight">{stay.city_name}</span>
        <span className="text-xs text-ink-3">{stationLabel(index)}</span>
        <span className="data text-xs text-ink-3">{stay.stay_days} 天</span>
      </div>
    </div>
  )
}

function DayBody({ day }: { day: DayOut }) {
  const hasItems = day.items.length > 0

  return (
    <div className="pb-5">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="data text-sm text-ink-2">{monthDay(day.date)}</span>
        <span className="text-xs text-ink-3">{weekday(day.date)}</span>
        {day.theme ? (
          <span className="font-display text-sm text-ink">{day.theme}</span>
        ) : null}
      </div>

      {hasItems ? (
        <ul className="m-0 mt-1.5 list-none space-y-1 p-0">
          {day.items.map((item, i) => (
            <ItemRow key={`${day.date}-${i}`} item={item} />
          ))}
        </ul>
      ) : (
        <p className="mt-1 text-xs text-ink-3">尚未安排</p>
      )}
    </div>
  )
}

function ItemRow({ item }: { item: ItemOut }) {
  // 景点没有实体主键，说明它还没对上高德 POI（ADR-0002），要么是待对齐、
  // 要么是待补坐标。这个状态必须让人看见，否则知识永远挂不上去。
  const awaitingAlignment = item.kind === 'poi' && !item.poi_id

  return (
    <li className="flex items-baseline gap-x-3 text-sm">
      <span className="data w-11 shrink-0 text-ink-3">{item.start_time ?? '——'}</span>

      {/* 时间轴要的是可扫读：地址单行截断，悬停看全文。
          高德的地址常带「(地铁站步行X分钟)」这类长后缀，任它折行会把节奏打散。 */}
      <span className="flex min-w-0 flex-1 items-baseline gap-x-2">
        <span className="shrink-0">{item.title}</span>
        {item.address ? (
          <span className="truncate text-xs text-ink-3" title={item.address}>
            {item.address}
          </span>
        ) : null}
      </span>

      {item.rating !== null ? (
        <span className="data shrink-0 text-xs text-ink-3">{item.rating.toFixed(1)}</span>
      ) : null}

      {awaitingAlignment ? (
        <span
          className="shrink-0 rounded-sm bg-cinnabar-soft px-1.5 py-0.5 text-xs text-cinnabar"
          title="这个景点还没对上实体真源，因此暂时挂不上攻略知识"
        >
          待对齐
        </span>
      ) : null}

      {item.kind !== 'poi' ? (
        <span className="shrink-0 text-xs text-ink-3">{kindLabel(item.kind)}</span>
      ) : null}
    </li>
  )
}
