import { useCallback, useEffect, useState } from 'react'

import {
  listAlignments,
  resolveAlignment,
  searchLocalPois,
  type AlignmentTaskOut,
  type LocalPoiOut,
} from '@/lib/api'

/**
 * 待对齐队列。
 *
 * 一条提及对不上高德时，光看它的名字人是没法判断的——必须同时看到
 * 「原文里它是怎么被说的」与「高德返回了什么、哪些被门禁挡掉了」。
 * 所以左边是候选结论与引文，右边是候选实体与拒绝理由。
 *
 * 一条红线：**不提供「自动选最高分」**。那正是设计里最危险的失败模式
 * （跨城挂错、挂到停车场）。这里只呈现，选哪个人来定。
 */
export function Alignments() {
  const [tasks, setTasks] = useState<AlignmentTaskOut[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [manual, setManual] = useState<LocalPoiOut[]>([])

  const load = useCallback(async () => {
    try {
      const rows = await listAlignments()
      setTasks(rows)
      setActiveId((current) =>
        current && rows.some((row) => row.task_id === current)
          ? current
          : (rows[0]?.task_id ?? null),
      )
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const active = tasks.find((task) => task.task_id === activeId) ?? null

  // 高德给回来的顺序是按相关度排的，但被门禁挡掉的那些会混在中间。
  // 能选的一律排前面——这一页要的是「挑一个」，不是「读完十个」。
  const ranked = active
    ? [...active.candidates].sort((a, b) => Number(b.usable) - Number(a.usable))
    : []
  // 已经在候选里出现过的实体不再重复列一遍
  const candidateIds = new Set(ranked.map((item) => item.poi_id))
  const extras = manual.filter((row) => !candidateIds.has(row.poi_id))

  useEffect(() => {
    if (!active) {
      setManual([])
      return
    }
    let alive = true
    searchLocalPois(active.mention_name)
      .then((rows) => {
        if (alive) setManual(rows)
      })
      .catch(() => {
        if (alive) setManual([])
      })
    return () => {
      alive = false
    }
  }, [active])

  const decide = async (payload: { poi_id?: string | null; discard?: boolean }) => {
    if (!active) return
    setBusy(true)
    try {
      await resolveAlignment(active.task_id, payload)
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  if (!tasks.length) {
    return (
      <p className="rounded border border-dashed border-rule px-4 py-6 text-sm text-ink-3">
        待对齐队列是空的。跑一轮管线之后，对不上高德的提及会落到这里。
      </p>
    )
  }

  return (
    <div className="space-y-5">
      <header>
        <h1 className="font-display text-2xl">待对齐</h1>
        <p className="mt-1 text-sm text-ink-2">
          {tasks.length} 条提及还没找到对应的高德实体。左边是网友怎么说的，右边是
          高德返回了什么。
        </p>
      </header>

      {error ? (
        <p className="rounded border border-cinnabar/30 bg-cinnabar-soft px-4 py-3 text-sm text-cinnabar">
          {error}
        </p>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-[16rem_minmax(0,1fr)]">
        <ol className="min-w-0 space-y-1.5">
          {tasks.map((task) => (
            <li key={task.task_id}>
              <button
                type="button"
                onClick={() => setActiveId(task.task_id)}
                className={[
                  'w-full rounded border px-3 py-2 text-left transition-colors',
                  task.task_id === activeId
                    ? 'border-azurite bg-azurite-soft'
                    : 'border-rule bg-paper-card hover:border-azurite/50',
                ].join(' ')}
              >
                <span className="font-display block text-sm text-ink">
                  {task.mention_name}
                </span>
                <span className="data mt-0.5 block text-[11px] text-ink-3">
                  {task.claims.length} 条结论
                </span>
              </button>
            </li>
          ))}
        </ol>

        {active ? (
          <section className="min-w-0 space-y-4">
            <div className="rounded border border-rule bg-paper-card p-5">
              <h2 className="font-display text-lg">「{active.mention_name}」</h2>
              <p className="data mt-1 text-xs text-ink-3">
                来自《{active.source_title ?? active.source_document_id ?? '未知素材'}》
                {active.city_adcode ? ` · 城市 ${active.city_adcode}` : ' · 缺城市线索'}
              </p>

              <ul className="mt-3 space-y-2">
                {active.claims.map((claim, index) => (
                  <li
                    key={`${claim.subject_name}-${index}`}
                    className="border-t border-rule pt-2 first:border-0 first:pt-0"
                  >
                    <p className="text-sm text-ink">{claim.text}</p>
                    {/* 结论与引文常常几乎一样（模型就是把原句改写成结论），
                        那就不要重复印两遍；不一样时才值得并排看 */}
                    {claim.quote.trim() && claim.quote.trim() !== claim.text.trim() ? (
                      <p className="mt-2 flex gap-2">
                        <span className="shrink-0 text-[11px] text-ink-3">原文</span>
                        <span className="font-display border-l-2 border-rule pl-3 text-xs leading-relaxed text-ink-2">
                          {claim.quote}
                        </span>
                      </p>
                    ) : null}
                    <p className="data mt-1 text-[11px] text-ink-3">
                      {claim.polarity === 'avoid' ? '避坑' : '打卡'} · {claim.facet}
                      {claim.quote_verdict && claim.quote_verdict !== 'exact'
                        ? ` · 引文${claim.quote_verdict}`
                        : ''}
                    </p>
                  </li>
                ))}
              </ul>
            </div>

            <div className="rounded border border-rule bg-paper-card p-5">
              <h2 className="text-sm text-ink">高德返回的候选</h2>
              {ranked.length ? (
                <ul className="mt-2 space-y-1.5">
                  {ranked.map((candidate) => (
                    <li
                      key={candidate.poi_id}
                      className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-t border-rule pt-1.5 first:border-0 first:pt-0"
                    >
                      <button
                        type="button"
                        disabled={busy || !candidate.usable}
                        onClick={() => decide({ poi_id: candidate.poi_id })}
                        className={[
                          'rounded border px-2 py-0.5 text-sm',
                          candidate.usable
                            ? 'border-rule text-ink hover:border-azurite'
                            : 'border-rule text-ink-3 line-through',
                        ].join(' ')}
                      >
                        {candidate.name}
                      </button>
                      <span className="data text-[11px] text-ink-3">{candidate.poi_id}</span>
                      {candidate.usable ? null : (
                        <span className="text-[11px] text-ink-3">
                          被挡：{candidate.reject_reason}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-sm text-ink-3">
                  高德没有返回像样的候选。可以判定它不是地点，或从下面库里已有的实体里挑。
                </p>
              )}

              {extras.length ? (
                <>
                  <h3 className="mt-4 text-xs text-ink-3">库里已有的同名实体</h3>
                  <ul className="mt-1.5 space-y-1">
                    {extras.map((row) => (
                      <li key={row.poi_id} className="flex flex-wrap items-baseline gap-3">
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => decide({ poi_id: row.poi_id })}
                          className="rounded border border-rule px-2 py-0.5 text-sm text-ink hover:border-azurite disabled:opacity-40"
                        >
                          {row.label}
                        </button>
                        <span className="data text-[11px] text-ink-3">
                          {row.is_root ? '本体' : '子点'} · {row.poi_id}
                        </span>
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}

              <div className="mt-4 flex flex-wrap gap-2 border-t border-rule pt-3">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => decide({ discard: true })}
                  className="rounded border border-rule px-3 py-1.5 text-sm text-ink-2 hover:border-cinnabar hover:text-cinnabar disabled:opacity-50"
                >
                  这不是一个地点
                </button>
                <p className="self-center text-xs text-ink-3">
                  判定为非地点会丢掉这 {active.claims.length} 条结论——它挂不上任何行程。
                </p>
              </div>
            </div>
          </section>
        ) : null}
      </div>
    </div>
  )
}
