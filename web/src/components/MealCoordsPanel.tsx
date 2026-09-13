import { useEffect, useState, type ReactNode } from 'react'

import {
  fetchMealCandidates,
  pinMeal,
  type MealCandidateOut,
  type MealCandidatesOut,
  type PendingMealOut,
  type PendingMealsOut,
} from '@/lib/api'
import { fullDate } from '@/lib/format'

/**
 * 没有坐标的餐饮项：人工指定那一步。
 *
 * 为什么需要这一块：路书里没有地图（ADR-0006），**路段说明就是空间关系的
 * 全部载体**，而一段路要两端都有坐标才算得出来。自动判据只写分得清的那些，
 * 剩下的是「绿柳居」这种——全城 24 家同名分店，名字一模一样，
 * 靠字面永远对不齐，靠「离得最近的那家赢」则会在一堆真分店里挑错一个。
 *
 * 人是怎么认出来的？不是靠名字分，是靠**「就在老门东里面」**。所以这一块
 * 把候选按「名字像不像、再按离当天景点多远」排好，并把那个景点名写在旁边。
 *
 * 三条刻意的做法：
 *
 * 1. **候选按需取**。列待办只查库；点开某一项才去打高德——一次页面加载打
 *    十几次外部接口，用户会以为界面卡死。
 * 2. **不按名字分筛候选**。「蒋有记(老门东店)」只有 0.34，可它就是答案；
 *    筛掉它等于把答案藏起来。分数照显，供参考。
 * 3. **「算不出距离」不显示成 0 米**。0 米看起来像就在旁边，那是在编。
 *
 * 排序还有一条订正：**距离不能单独当排序键**。第一版只按距离排，实测高德的
 * 周边搜索会带回附近别的饭馆（搜「南京大牌档（中山陵店）」，紫金坊边上的
 * 火烧店、面馆全进来了），它们离得更近，把真正像的那几家挤到后面去。
 * 现在名字分是主序、距离是次序。
 */
export function MealCoordsPanel({
  data,
  error,
  tripId,
  onPinned,
}: {
  data: PendingMealsOut | null
  error: string | null
  tripId: string
  /** 指定成功后把刷新后的清单交回给行程页——它同时要把坐标标到天项上。 */
  onPinned: (fresh: PendingMealsOut) => void
}) {
  const [openId, setOpenId] = useState<string | null>(null)

  if (error) {
    return (
      <Shell>
        <p className="mt-2 text-sm text-ink-3">拿不到待补清单：{error}</p>
      </Shell>
    )
  }
  if (!data) {
    return (
      <Shell>
        <p className="mt-2 text-sm text-ink-3">读取中…</p>
      </Shell>
    )
  }
  if (!data.items.length) return null

  return (
    <Shell>
      <p className="mt-2 text-sm leading-relaxed text-ink-2">
        这 {data.items.length} 处没有坐标，路书里只能写「按名字问路」。
        指定之后就有了路段说明与导航。
      </p>
      <ul className="mt-3 space-y-2">
        {data.items.map((meal) => (
          <li key={meal.item_id}>
            <MealRow
              meal={meal}
              tripId={tripId}
              open={openId === meal.item_id}
              onToggle={() => setOpenId(openId === meal.item_id ? null : meal.item_id)}
              onPinned={(fresh) => {
                setOpenId(null)
                onPinned(fresh)
              }}
            />
          </li>
        ))}
      </ul>
    </Shell>
  )
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <section className="rounded border border-rule bg-paper-card p-5">
      <h2 className="font-display text-xl">没有坐标的餐饮项</h2>
      {children}
    </section>
  )
}

function MealRow({
  meal,
  tripId,
  open,
  onToggle,
  onPinned,
}: {
  meal: PendingMealOut
  tripId: string
  open: boolean
  onToggle: () => void
  onPinned: (fresh: PendingMealsOut) => void
}) {
  return (
    <div className="rounded border border-rule/60 px-3 py-2">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <span className="data mr-2 text-xs text-ink-3">{fullDate(meal.day_date)}</span>
          <span className="text-sm text-ink">{meal.title}</span>
          {meal.anchors.length ? (
            <span className="ml-2 text-xs text-ink-3">
              当天的 {meal.anchors.join('、')} 附近
            </span>
          ) : null}
        </div>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="shrink-0 rounded border border-rule px-2 py-0.5 text-xs text-ink-2 hover:border-azurite"
        >
          {open ? '收起' : '选一个'}
        </button>
      </div>
      {open ? <Candidates tripId={tripId} itemId={meal.item_id} onPinned={onPinned} /> : null}
    </div>
  )
}

function Candidates({
  tripId,
  itemId,
  onPinned,
}: {
  tripId: string
  itemId: string
  onPinned: (fresh: PendingMealsOut) => void
}) {
  const [data, setData] = useState<MealCandidatesOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // 打开即取，只取一次（组件随「收起」卸载，再打开会重新取）。
  // 不复用一份缓存的候选是有意的：高德的排序会变，让人对着过期的东西挑
  // 比多打一次接口糟。
  useEffect(() => {
    let alive = true
    fetchMealCandidates(tripId, itemId)
      .then((fresh) => {
        if (alive) setData(fresh)
      })
      .catch((cause: unknown) => {
        if (alive) setError(cause instanceof Error ? cause.message : String(cause))
      })
    return () => {
      alive = false
    }
  }, [tripId, itemId])

  const choose = (candidate: MealCandidateOut) => {
    setBusy(true)
    pinMeal(tripId, itemId, {
      lat_gcj02: candidate.lat_gcj02,
      lng_gcj02: candidate.lng_gcj02,
      address: candidate.address,
    })
      .then(onPinned)
      .catch((cause: unknown) => setError(cause instanceof Error ? cause.message : String(cause)))
      .finally(() => setBusy(false))
  }

  if (error) return <p className="mt-2 text-xs text-cinnabar">取候选失败：{error}</p>
  if (!data) return <p className="mt-2 text-xs text-ink-3">正在问高德…</p>
  if (!data.candidates.length) {
    return (
      <p className="mt-2 text-xs text-ink-3">
        高德上没搜到这家店{data.note ? `（${data.note}）` : ''}。这类地方只能到当地按名字问。
      </p>
    )
  }

  return (
    <div className="mt-2">
      <ul className="space-y-1">
        {data.candidates.map((candidate) => (
          <li key={candidate.poi_id} className="flex items-baseline justify-between gap-3">
            <div className="min-w-0">
              <p className="text-sm text-ink-2">{candidate.name}</p>
              <p className="data text-[11px] text-ink-3">
                {distanceNote(candidate)}
                {candidate.address ? ` · ${candidate.address}` : ''}
                {` · 名字吻合 ${candidate.name_score.toFixed(2)}`}
              </p>
            </div>
            <button
              type="button"
              disabled={busy}
              onClick={() => choose(candidate)}
              className="shrink-0 rounded border border-rule px-2 py-0.5 text-xs text-ink-2 hover:border-malachite disabled:opacity-40"
            >
              就这个
            </button>
          </li>
        ))}
      </ul>
      {data.total > data.candidates.length ? (
        <p className="data mt-1 text-[11px] text-ink-3">另有 {data.total - data.candidates.length} 个更远的</p>
      ) : null}
    </div>
  )
}

/** 距离那句话。**算不出就说算不出**，不写成 0 米。 */
function distanceNote(candidate: MealCandidateOut): string {
  if (candidate.distance_m === null) return '距离算不出（当天没有带坐标的景点）'
  const metres = candidate.distance_m
  const shown = metres >= 1000 ? `${(metres / 1000).toFixed(1)} 公里` : `${metres} 米`
  return candidate.nearest_anchor ? `离${candidate.nearest_anchor} ${shown}` : `离当天景点 ${shown}`
}
