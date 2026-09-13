import { useCallback, useEffect, useState } from 'react'

import { listExtractions, type ExtractionRunOut } from '@/lib/api'

/**
 * 提纯概览。
 *
 * 这一页只回答一个问题：**模型有没有编原文里没有的话。**
 * `dropped_count` 不为零的篇要一眼看见——那是被引文校验挡下来的条数，
 * 也是「提纯不产生新事实」这条设计的体检指标。为零是常态，
 * 所以界面上不能把它做成一个恒亮的小红点，而要让它**出现时才是异常**。
 */
export function Extractions() {
  const [runs, setRuns] = useState<ExtractionRunOut[]>([])
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setRuns(await listExtractions())
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (error) {
    return (
      <p className="rounded border border-cinnabar/30 bg-cinnabar-soft px-4 py-3 text-sm text-cinnabar">
        {error}
      </p>
    )
  }

  const suspicious = runs.filter((run) => run.dropped_count > 0)
  const failed = runs.filter((run) => run.status !== 'ok')

  return (
    <div className="space-y-5">
      <header>
        <h1 className="font-display text-2xl">提纯</h1>
        <p className="mt-1 text-sm text-ink-2">
          每一次提纯运行：模型吐了几条、引文有几条回到了原文。
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-3">
        <Tally label="运行次数" value={runs.length} note="含命中缓存的重复跑" />
        <Tally
          label="引文对不上原文"
          value={suspicious.reduce((sum, run) => sum + run.dropped_count, 0)}
          note={
            suspicious.length
              ? `${suspicious.length} 篇出现过，点开看`
              : '全部回到原文，这是常态'
          }
          alarming={suspicious.length > 0}
        />
        <Tally
          label="失败的篇"
          value={failed.length}
          note={failed.length ? '看下面的错误' : '没有失败'}
          alarming={failed.length > 0}
        />
      </div>

      {suspicious.length ? (
        <section className="rounded border border-azurite bg-azurite-soft/50 p-4">
          <h2 className="text-sm text-ink">这几篇模型编了引文</h2>
          <ul className="mt-2 space-y-1 text-sm text-ink-2">
            {suspicious.map((run) => (
              <li key={run.run_id}>
                <span className="font-display">{run.source_title ?? run.source_document_id}</span>
                <span className="data ml-2 text-xs text-ink-3">
                  丢弃 {run.dropped_count} / 吐出 {run.candidate_count}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs leading-relaxed text-ink-3">
            丢弃的候选没有进知识库。若某篇丢得特别多，值得回去看一眼正文是不是被清洗坏了——
            引文是在清洗后的正文里定位的。
          </p>
        </section>
      ) : null}

      {runs.length ? (
        <div className="min-w-0 overflow-x-auto">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-rule text-left text-xs text-ink-3">
                <th className="py-2 pr-3 font-normal">素材</th>
                <th className="py-2 pr-3 font-normal">模型</th>
                <th className="py-2 pr-3 text-right font-normal">吐出</th>
                <th className="py-2 pr-3 text-right font-normal">通过</th>
                <th className="py-2 pr-3 text-right font-normal">丢弃</th>
                <th className="py-2 pr-3 text-right font-normal">耗时</th>
                <th className="py-2 font-normal">时间</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id} className="border-b border-rule/60">
                  <td className="max-w-64 truncate py-2 pr-3">
                    {run.source_title ?? run.source_document_id}
                  </td>
                  <td className="data py-2 pr-3 text-xs text-ink-2">{run.model}</td>
                  <td className="data py-2 pr-3 text-right">{run.candidate_count}</td>
                  <td className="data py-2 pr-3 text-right">{run.accepted_count}</td>
                  <td
                    className={[
                      'data py-2 pr-3 text-right',
                      run.dropped_count > 0 ? 'text-azurite' : 'text-ink-3',
                    ].join(' ')}
                  >
                    {run.dropped_count}
                  </td>
                  <td className="data py-2 pr-3 text-right text-xs text-ink-2">
                    {run.duration_ms === null ? '缓存' : `${(run.duration_ms / 1000).toFixed(1)}s`}
                  </td>
                  <td className="data py-2 text-xs text-ink-3">{run.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="rounded border border-dashed border-rule px-4 py-6 text-sm text-ink-3">
          还没有提纯记录。导一篇素材进来，再跑一轮管线。
        </p>
      )}
    </div>
  )
}

function Tally({
  label,
  value,
  note,
  alarming = false,
}: {
  label: string
  value: number
  note: string
  alarming?: boolean
}) {
  return (
    <section className="rounded border border-rule bg-paper-card px-4 py-3">
      <p className="text-xs text-ink-3">{label}</p>
      <p className={`data mt-1 text-2xl ${alarming ? 'text-azurite' : 'text-ink'}`}>{value}</p>
      <p className="mt-0.5 text-[11px] text-ink-3">{note}</p>
    </section>
  )
}
