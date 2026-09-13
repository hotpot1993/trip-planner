import { useState, type ReactNode } from 'react'

import { hrefFor } from '@/lib/router'
import type {
  DayOut,
  InsightOut,
  ItemInsightsOut,
  ItemOut,
  StayOut,
  TransferOut,
  TripInsightsOut,
} from '@/lib/api'
import { kindLabel, monthDay, weekday } from '@/lib/format'

/**
 * 驿道线：行程的签名元素。
 *
 * 一条连续的竖线从第一站贯穿到最后一天，站点与天都是挂在线上的节点。
 * 它不只是装饰——线本身编码了信息：
 *
 * - 圆点的大小区分「城市停留」与「一天」
 * - 圆点的实心 / 空心区分「已安排」与「未安排」
 * - **城际转移画在它落到的那一天里面**，而不是两个城市之间的独立节点：
 *   转移是那一天的一部分（ADR-0004），画在站与站之间会让人以为它不占当天。
 *
 * 把城市与天放进同一个扁平序列，是为了让这条线真的连续。嵌套列表在视觉上
 * 会断成几截，那样它就退化成普通的日程列表了。
 */

type RailNode =
  | { kind: 'station'; key: string; stay: StayOut; index: number }
  | { kind: 'day'; key: string; stay: StayOut; day: DayOut; dayIndex: number }

const CN_NUMERALS = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'] as const

function stationLabel(index: number): string {
  return `第${CN_NUMERALS[index] ?? String(index + 1)}站`
}

function buildNodes(stays: StayOut[]): RailNode[] {
  const nodes: RailNode[] = []
  let dayIndex = 0

  stays.forEach((stay, index) => {
    nodes.push({ kind: 'station', key: `station-${index}`, stay, index })
    stay.days.forEach((day) => {
      nodes.push({
        kind: 'day',
        key: `day-${index}-${day.date}`,
        stay,
        day,
        dayIndex,
      })
      dayIndex += 1
    })
  })
  return nodes
}

export function RouteRail({
  stays,
  transfers = [],
  insights,
}: {
  stays: StayOut[]
  transfers?: TransferOut[]
  /** 按 POI 分组的软经验。不注入模型，生成后挂载（设计 5.4）。 */
  insights?: TripInsightsOut
}) {
  const nodes = buildNodes(stays)

  if (nodes.length === 0) {
    return (
      <p className="rounded border border-dashed border-rule px-4 py-6 text-sm text-ink-3">
        这份行程还没有城市。先在下方添加一座，或者用规划生成。
      </p>
    )
  }

  const byDay = new Map<number, TransferOut[]>()
  for (const transfer of transfers) {
    const list = byDay.get(transfer.day_index)
    if (list) list.push(transfer)
    else byDay.set(transfer.day_index, [transfer])
  }

  const hasAnyItem = nodes.some(
    (node) =>
      node.kind === 'day' &&
      (node.day.items.length > 0 || byDay.has(node.dayIndex)),
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
          <RailRow
            key={node.key}
            node={node}
            isLast={index === nodes.length - 1}
            transfers={node.kind === 'day' ? (byDay.get(node.dayIndex) ?? []) : []}
            insights={insights}
          />
        ))}
      </ol>
    </div>
  )
}

function RailRow({
  node,
  isLast,
  transfers,
  insights,
}: {
  node: RailNode
  isLast: boolean
  transfers: TransferOut[]
  insights?: TripInsightsOut
}) {
  const hasContent = node.kind === 'day' ? node.day.items.length > 0 || transfers.length > 0 : true

  return (
    <li className="grid grid-cols-[26px_1fr]">
      <RailGutter
        line={!isLast}
        marker={node.kind === 'station' ? <StationDot /> : <DayTick filled={hasContent} />}
      />
      {node.kind === 'station' ? (
        <StationBody node={node} />
      ) : (
        <DayBody day={node.day} transfers={transfers} insights={insights} />
      )}
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
        {/* 城市名链到城市页：那是最自然的入口——读到「西安 3 天」时，
            下一个问题就是「西安有什么可去的」。 */}
        {stay.city_adcode ? (
          <a
            href={hrefFor({ name: 'city', adcode: stay.city_adcode, cityName: stay.city_name })}
            className="font-display text-lg leading-tight text-ink underline decoration-rule decoration-1 underline-offset-4 hover:decoration-azurite"
          >
            {stay.city_name}
          </a>
        ) : (
          <span className="font-display text-lg leading-tight">{stay.city_name}</span>
        )}
        <span className="text-xs text-ink-3">{stationLabel(index)}</span>
        <span className="data text-xs text-ink-3">{stay.stay_days} 天</span>
        {stay.city_adcode ? (
          <a
            href={hrefFor({ name: 'city', adcode: stay.city_adcode, cityName: stay.city_name })}
            className="text-xs text-ink-3 no-underline hover:text-ink"
          >
            有哪些可去的 →
          </a>
        ) : null}
      </div>
    </div>
  )
}

function DayBody({
  day,
  transfers,
  insights,
}: {
  day: DayOut
  transfers: TransferOut[]
  insights?: TripInsightsOut
}) {
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

      {transfers.map((transfer) => (
        <TransferBlock key={transfer.id} transfer={transfer} />
      ))}

      {hasItems ? (
        <ul className="m-0 mt-1.5 list-none space-y-1 p-0">
          {day.items.map((item, i) => (
            <ItemRow
              key={`${day.date}-${i}`}
              item={item}
              insights={item.poi_id ? insights?.by_poi[item.poi_id] : undefined}
            />
          ))}
        </ul>
      ) : transfers.length === 0 ? (
        <p className="mt-1 text-xs text-ink-3">尚未安排</p>
      ) : null}
    </div>
  )
}

/** 城际转移。它落在这一天里，所以画在这一天里。 */
function TransferBlock({ transfer }: { transfer: TransferOut }) {
  const [expanded, setExpanded] = useState(false)
  const minutes = transfer.duration_min
  const duration = minutes === null ? null : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`

  return (
    <div className="mt-2 rounded border border-azurite/30 bg-azurite-soft/40 px-3 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
        <span className="text-azurite">⇢</span>
        <span className="text-ink">
          {transfer.from_city_name} → {transfer.to_city_name}
        </span>
        <span className="text-xs text-ink-3">{modeLabel(transfer.mode)}</span>
      </div>

      {transfer.service_no ? (
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 text-sm">
          <span className="data text-ink">{transfer.service_no}</span>
          <span className="text-ink-2">
            {transfer.from_station} → {transfer.to_station}
          </span>
          <span className="data text-ink-2">
            {transfer.dep_time}–{transfer.arr_time}
          </span>
          {duration ? <span className="data text-xs text-ink-3">{duration}</span> : null}
          {transfer.price !== null ? (
            <span className="data text-sm text-ink">
              ¥{transfer.price}
              {transfer.is_reference_price ? (
                <span className="ml-1 text-xs text-ink-3">参考价</span>
              ) : null}
            </span>
          ) : null}
          {transfer.has_tickets === false ? (
            <span className="text-xs text-ink-3">查询时已无票</span>
          ) : null}
        </div>
      ) : (
        <p className="mt-1 text-sm text-ink-2">还没有查到具体车次</p>
      )}

      {transfer.advice_reason ? (
        <p className="mt-1 text-xs leading-relaxed text-ink-2">{transfer.advice_reason}</p>
      ) : null}

      {transfer.note ? (
        <p className="mt-1 text-xs leading-relaxed text-ink-3">{transfer.note}</p>
      ) : null}

      {transfer.alternatives.length > 0 ? (
        <>
          <button
            type="button"
            className="mt-1.5 text-xs text-azurite underline"
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? '收起备选' : `备选 ${transfer.alternatives.length} 个`}
          </button>
          {expanded ? (
            <ul className="m-0 mt-1 list-none space-y-0.5 p-0">
              {transfer.alternatives.map((option) => {
                const mins = option.duration_min
                return (
                  <li key={option.service_no} className="flex flex-wrap gap-x-3 text-xs text-ink-2">
                    <span className="data">{option.service_no}</span>
                    <span className="data">
                      {option.dep_time}–{option.arr_time}
                    </span>
                    {mins !== null ? (
                      <span className="data">
                        {Math.floor(mins / 60)}h{mins % 60}′
                      </span>
                    ) : null}
                  </li>
                )
              })}
            </ul>
          ) : null}
        </>
      ) : null}
    </div>
  )
}

function modeLabel(mode: string): string {
  switch (mode) {
    case 'rail':
      return '铁路'
    case 'air':
      return '航空'
    case 'coach':
      return '大巴'
    case 'drive':
      return '自驾'
    default:
      return mode
  }
}

function ItemRow({ item, insights }: { item: ItemOut; insights?: ItemInsightsOut }) {
  const [open, setOpen] = useState(false)

  // 景点没有实体主键，说明它还没对上高德 POI（ADR-0002），要么是待对齐、
  // 要么是待补坐标。这个状态必须让人看见，否则知识永远挂不上去。
  const awaitingAlignment = item.kind === 'poi' && !item.poi_id
  const hasInsights = Boolean(insights && (insights.highlights.length || insights.avoids.length))

  return (
    <li className="text-sm">
      <div className="flex items-baseline gap-x-3">
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

        {/* 软经验**不进正文**：整段攻略铺进时间轴，扫读就没了。
            所以这里是「几个标记 + 展开」——数量看得见，原文点开才读。 */}
        {hasInsights ? (
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            aria-expanded={open}
            className="shrink-0 rounded border border-rule px-1.5 py-0.5 text-[11px] text-ink-2 hover:border-azurite"
            title="网友对这个地方说过的话"
          >
            {insights!.avoids.length ? (
              <span className="text-azurite">{insights!.avoids.length} 避坑</span>
            ) : null}
            {insights!.avoids.length && insights!.highlights.length ? ' · ' : ''}
            {insights!.highlights.length ? (
              <span className="text-malachite">{insights!.highlights.length} 打卡</span>
            ) : null}
          </button>
        ) : null}

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
      </div>

      {open && insights ? <InsightList insights={insights} /> : null}
    </li>
  )
}

/**
 * 展开之后的原文。
 *
 * **原文照登，不改一个字**——这些是网友说过的话，任何转述都会让它
 * 从「证据」变成「我们的说法」。置信度跟着每条一起显示：
 * 「3 个独立来源」与「待验证的个例」是这份攻略可信度的全部依据。
 */
function InsightList({ insights }: { insights: ItemInsightsOut }) {
  const groups: [string, InsightOut[], string][] = [
    ['避坑', insights.avoids, 'border-azurite text-azurite'],
    ['打卡', insights.highlights, 'border-malachite text-malachite'],
  ]
  return (
    <div className="mt-1.5 ml-14 space-y-2">
      {groups.map(([title, claims, tone]) =>
        claims.length ? (
          <div key={title}>
            <p className={`text-[11px] ${tone.split(' ')[1]}`}>{title}</p>
            <ul className="mt-0.5 space-y-1">
              {claims.map((claim) => (
                <li
                  key={claim.claim_id}
                  className={`border-l-2 pl-2.5 ${tone.split(' ')[0]}`}
                >
                  <p className="text-[13px] leading-relaxed text-ink-2">{claim.text}</p>
                  <p className="data mt-0.5 text-[10px] text-ink-3">
                    {claim.single_source
                      ? '待验证的个例（只有 1 个来源）'
                      : `${claim.independent_source_count} 个独立来源`}
                  </p>
                </li>
              ))}
            </ul>
          </div>
        ) : null,
      )}
    </div>
  )
}
