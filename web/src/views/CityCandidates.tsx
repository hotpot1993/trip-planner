import { useEffect, useState } from 'react'

import { fetchCityPool, type CandidateOut, type CityPoolOut, type ClaimBriefOut } from '@/lib/api'
import { describeDue, fullDate } from '@/lib/format'

/**
 * 城市页：这座城市有什么可去的。
 *
 * M5 的验收条件是「打开城市看到的是网友推荐而非一片 POI」。这一页就是那句话
 * 的落点，所以它的形状由一个问题决定：**一片 POI 缺的是什么？**
 *
 * 缺的是观点。高德会告诉你「这里有个公园」，不会告诉你「傍晚上去最好，
 * 白天没遮阴」。所以每条候选的主体不是「名称 + 评分 + 距离」，
 * 而是**打卡建议与避坑指南本身**——原文照登，带置信度。
 *
 * 三条刻意的取舍：
 *
 * 1. **打卡与避坑分开排**，不混成一段。它们是两类信息：「值得去」与
 *    「注意什么」，揉在一起就变成一句含糊的话。
 * 2. **来源必须看得见。** 一条候选是「三个网友推荐过」还是「高德搜出来的」，
 *    对用户的意义完全不同——把一片地图 POI 说成「推荐」是这套东西
 *    最不该犯的错。所以回落高德时整页换一种说法。
 * 3. **覆盖度说实话。** 知识库还很薄时要说「还很少」，不能让一个空池子
 *    冒充「这座城市没什么可去的」。
 */
export function CityCandidates({ adcode, name }: { adcode: string; name?: string }) {
  const [pool, setPool] = useState<CityPoolOut | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setPool(null)
    setError(null)
    fetchCityPool(adcode, { name })
      .then((data) => {
        if (alive) setPool(data)
      })
      .catch((cause: unknown) => {
        if (alive) setError(cause instanceof Error ? cause.message : String(cause))
      })
    return () => {
      alive = false
    }
  }, [adcode, name])

  if (error) {
    return (
      <div className="space-y-4">
        <h1 className="font-display text-3xl">{name ?? adcode}</h1>
        <p className="rounded border border-rule bg-paper-card px-4 py-3 text-sm text-ink-2">
          {error}
        </p>
      </div>
    )
  }

  if (!pool) return <p className="text-sm text-ink-3">读取中…</p>

  const recommended = pool.candidates.filter((item) => item.recommended)
  const onlyMap = pool.candidates.length > 0 && recommended.length === 0

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="font-display text-3xl">{name ?? pool.city_adcode}</h1>
        {onlyMap ? (
          <p className="max-w-2xl text-sm leading-relaxed text-ink-2">
            这座城市还没有攻略数据，下面是
            <strong className="font-normal text-ink">地图上有的地方</strong>
            ——它们只是存在，还没有网友推荐过。把「网友推荐」与「地图上有」
            混为一谈，正是这类工具最容易骗人的地方。
          </p>
        ) : (
          <p className="max-w-2xl text-sm leading-relaxed text-ink-2">
            {recommended.length} 个地方有网友写过。
            {pool.covered
              ? ''
              : '这座城市的数据还很少，够不上支撑一次完整的规划——多导入几篇攻略会好起来。'}
          </p>
        )}
      </header>

      {pool.amap_error ? (
        // 这句话原先写的是「高德补全没成功：…。缺坐标的地方在地图上暂时不可用。」
        // ——两处都不对：`amap_error` 只在**回落搜索**失败时被设置（补全那条路
        // 目前没有调用方），而且这种时候下面**一条候选都没有**，不是「缺坐标的
        // 地方不可用」。说成「没查到」而不是「没有」，是这一页最要紧的区别。
        <p className="rounded border border-rule bg-paper-card px-4 py-3 text-xs text-ink-3">
          这座城市还没有攻略数据，去高德搜也没成功：{pool.amap_error}。
          所以下面没有候选——这跟「这里没什么可去的」不是一回事。
        </p>
      ) : null}

      {pool.candidates.length ? (
        <ul className="grid gap-4 lg:grid-cols-2">
          {pool.candidates.map((item) => (
            <CandidateCard key={item.poi_id} item={item} />
          ))}
        </ul>
      ) : (
        <p className="rounded border border-dashed border-rule px-4 py-6 text-sm leading-relaxed text-ink-3">
          这座城市还没有任何攻略数据。到数据工作台导入几篇攻略，跑一轮管线，
          网友推荐过的地方就会出现在这里。
        </p>
      )}
    </div>
  )
}

/**
 * 预约那一行的文案。
 *
 * 三种状态必须写得出来，而且不能混：
 *
 * - 要预约、有确切口径 → 「需预约，提前 7 天 20:00」
 * - 要预约、**官方没公布口径** → 「需预约，放票口径以官方为准」。
 *   写成「无预约信息」等于把「必须预约」说成了「不用管」——实测踩过，
 *   兵马俑那条规则就是这样被显示成没有信息的。
 * - 不需要预约 → 「不需要预约」（这是已复核规则的结论，不是「不知道」）
 *
 * 「没有已复核的规则」的那种情况走不到这里——调用方据此不渲染这一行。
 */
function bookingLabel(item: CandidateOut): string {
  if (!item.booking_required) return '不需要预约'
  const parts: string[] = []
  if (item.booking_days !== null) parts.push(`提前 ${item.booking_days} 天`)
  if (item.booking_time) parts.push(item.booking_time)
  if (!parts.length) return '需预约，放票口径以官方为准'
  return `需预约，${parts.join(' ')}`
}

function CandidateCard({ item }: { item: CandidateOut }) {
  return (
    <li className="flex flex-col rounded border border-rule bg-paper-card p-5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="font-display text-lg text-ink">{item.name}</h2>
        {item.recommended ? (
          <span className="data text-xs text-ink-3">
            {item.highlights.length} 打卡 · {item.avoids.length} 避坑
          </span>
        ) : (
          <span className="rounded bg-paper-sunk px-2 py-0.5 text-[11px] text-ink-3">
            地图上有的地方
          </span>
        )}
      </div>

      <dl className="mt-1 flex flex-wrap items-baseline gap-x-4 text-[11px] text-ink-3">
        {item.rating !== null ? (
          <div className="flex gap-1">
            <dt className="sr-only">评分</dt>
            <dd className="data">{item.rating.toFixed(1)} 分</dd>
          </div>
        ) : null}
        {/* 高德的 open_time 经常是「6月4日-11月14日 08:30-19:00(18:00停止检票);
            11月15日至次年3月14日 全天开放…」这种长串。整串铺在卡片顶部会把
            真正该看的东西（网友的话）挤下去，所以折起来，要看再展开。 */}
        {item.open_time ? (
          <div className="min-w-0">
            <dt className="sr-only">开放时间</dt>
            <dd>
              <details>
                <summary className="data cursor-pointer">开放时间</summary>
                <span className="data mt-1 block max-w-md leading-relaxed">
                  {item.open_time}
                </span>
              </details>
            </dd>
          </div>
        ) : null}
        {item.booking_required !== null ? (
          <div className="flex gap-1">
            <dt className="sr-only">预约</dt>
            <dd className={item.booking_required ? 'text-azurite' : 'text-ink-3'}>
              {bookingLabel(item)}
            </dd>
          </div>
        ) : null}
      </dl>

      {item.recommended ? (
        <div className="mt-3 space-y-3">
          <ClaimGroup
            title="打卡建议"
            claims={item.highlights}
            tone="highlight"
          />
          <ClaimGroup title="避坑指南" claims={item.avoids} tone="avoid" />
        </div>
      ) : (
        <p className="mt-3 text-xs leading-relaxed text-ink-3">
          还没有网友写过它。地图能提供坐标和开放时间，提供不了「什么时候去、
          要注意什么」。
        </p>
      )}
    </li>
  )
}

/**
 * 一组结论。
 *
 * 置信度直接写在每条旁边：「3 个来源」与「1 个来源」的区别，是这份攻略
 * 可信度的全部依据。设计里说的是「标为待验证个例而不是丢弃」——
 * 单源的说法照样显示，但要看得出它是单源。
 *
 * 那句话**不在这里拼**，用服务端给的 `confidence_text`。原先这里与行程页
 * 各写了一遍同一个三元表达式，其中一份还把 2 个来源说成「只有 1 个来源」
 * ——而门槛是 3。措辞只在 `domain.knowledge.describe_confidence` 定一次。
 *
 * 每组最多铺三条。陕西历史博物馆那样一个地方有九条结论时，全铺出来会
 * 把旁边的卡片一起挤成一条长条，「这座城市有什么可去的」反而看不见了。
 */
const CLAIMS_SHOWN = 3

/**
 * 复验那一句。到期只标注不隐藏（设计 4.6）——过期的经验仍然展示，
 * 但用户得知道自己看的是旧信息。还没到期的照常印出到期日，
 * 「还有多久要重核」本身也是有用的信息。
 */
function dueNote(iso: string | null): string {
  const due = describeDue(iso)
  if (due) return ` · ${due}`
  return iso ? ` · 复验到期 ${fullDate(iso)}` : ''
}

function ClaimGroup({
  title,
  claims,
  tone,
}: {
  title: string
  claims: ClaimBriefOut[]
  tone: 'highlight' | 'avoid'
}) {
  if (!claims.length) return null
  const accent = tone === 'highlight' ? 'border-malachite' : 'border-azurite'
  const shown = claims.slice(0, CLAIMS_SHOWN)
  const rest = claims.length - shown.length
  return (
    <section>
      <h3 className={`text-xs ${tone === 'highlight' ? 'text-malachite' : 'text-azurite'}`}>
        {title}
      </h3>
      <ul className="mt-1 space-y-1.5">
        {shown.map((claim) => (
          <li key={claim.claim_id} className={`border-l-2 pl-3 ${accent}`}>
            <p className="text-sm leading-relaxed text-ink">{claim.text}</p>
            <p className="data mt-0.5 text-[10px] text-ink-3">
              {claim.confidence_text}
              {dueNote(claim.verify_due_at)}
            </p>
          </li>
        ))}
      </ul>
      {rest > 0 ? (
        <p className="data mt-1 text-[11px] text-ink-3">另有 {rest} 条</p>
      ) : null}
    </section>
  )
}
