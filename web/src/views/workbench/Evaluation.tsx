import { useCallback, useEffect, useState } from 'react'

import { runEvaluation, type EvalOut, type GoldScoreOut } from '@/lib/api'

/**
 * 评测。
 *
 * 这一页的排版由一件事决定：**数字必须与样本量同时出现。**
 * 三篇素材算出来的 100% 精确率是噪声，而一个孤零零的百分比会被人当结论用。
 * 所以样本量不是脚注，它和三张卡片同级。
 *
 * 另一件事同样重要：算不出来与零分是两回事。没有标注时精确率是「判不出」，
 * 显示 0% 会被读成「抽的一条都没对」。
 */
export function Evaluation() {
  const [data, setData] = useState<EvalOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setBusy(true)
    try {
      setData(await runEvaluation())
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
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

  if (!data) return <p className="text-sm text-ink-3">跑评测中…</p>

  const empty = data.sample_size === 0

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div>
          <h1 className="font-display text-2xl">评测</h1>
          <p className="mt-1 text-sm text-ink-2">
            提纯抽得全不全，对齐挂得对不对。分母只含已标注的素材。
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={busy}
          className="rounded border border-rule px-3 py-1.5 text-sm text-ink-2 hover:border-azurite disabled:opacity-50"
        >
          {busy ? '跑…' : '重跑'}
        </button>
      </header>

      {empty ? (
        <p className="rounded border border-dashed border-rule px-4 py-6 text-sm leading-relaxed text-ink-3">
          金标准集是空的，没有可评的东西。到「标注」页标一篇、并点「标完了」，
          这里才有分母。
        </p>
      ) : (
        <>
          {!data.ready ? (
            <section className="rounded border border-azurite bg-azurite-soft/50 px-4 py-3">
              <p className="text-sm text-ink">
                样本不足：{data.sample_size} 篇，设计要 30 篇。
              </p>
              <p className="mt-1 text-xs leading-relaxed text-ink-2">
                下面这些数字能说明链路是通的，但
                <strong className="font-normal text-ink">不足以支撑结论</strong>
                ——一次调优带来的涨跌可能全在噪声里。
              </p>
            </section>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-3">
            <Metric
              label="抽取召回率"
              value={data.recall}
              detail={`${data.hits}/${data.gold_total} 条人工结论被抽到`}
            />
            <Metric
              label="抽取精确率"
              value={data.precision}
              detail={`抽了 ${data.predicted_total} 条，${data.hits} 条对得上`}
            />
            <Metric
              label="对齐准确率"
              value={data.alignment_accuracy}
              detail={
                data.unaligned
                  ? `${data.unaligned} 条压根没挂上，已计入错误`
                  : '全都挂上了'
              }
            />
          </div>

          <section className="rounded border border-rule bg-paper-card p-4">
            <h2 className="text-sm text-ink">这批数字是怎么来的</h2>
            <dl className="mt-2 grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
              <Row label="已标注素材" value={`${data.gold_documents} 篇`} />
              <Row label="人工结论" value={`${data.gold_labels} 条`} />
              <Row
                label="盲标篇数"
                value={`${data.blind_documents} 篇`}
                hint="盲标更可信：先自己读完写结论，再拿模型结果对照"
              />
              <Row
                label="人工判定非地点"
                value={data.discard_ratio === null ? '判不出' : `${Math.round(data.discard_ratio * 100)}%`}
                hint="比例高说明上游把「排队」「门票」这类词也当成了景点名"
              />
            </dl>
          </section>

          {data.documents.map((item) => (
            <DocumentScore key={item.document_id} score={item} />
          ))}
        </>
      )}
    </div>
  )
}

/** 比率转百分比。`null` 是「算不出来」，不是 0。 */
function percent(value: number | null): string {
  return value === null ? '判不出' : `${(value * 100).toFixed(1)}%`
}

function Metric({
  label,
  value,
  detail,
}: {
  label: string
  value: number | null
  detail: string
}) {
  return (
    <section className="rounded border border-rule bg-paper-card px-4 py-3">
      <p className="text-xs text-ink-3">{label}</p>
      <p
        className={[
          'data mt-1 text-3xl',
          value === null ? 'text-ink-3' : 'text-ink',
        ].join(' ')}
      >
        {percent(value)}
      </p>
      <p className="mt-0.5 text-[11px] text-ink-3">{detail}</p>
    </section>
  )
}

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex gap-3">
      <dt className="w-28 shrink-0 text-ink-3">{label}</dt>
      <dd className="text-ink-2">
        {value}
        {hint ? <span className="ml-2 text-[11px] text-ink-3">{hint}</span> : null}
      </dd>
    </div>
  )
}

function DocumentScore({ score }: { score: GoldScoreOut }) {
  const weak =
    (score.recall !== null && score.recall < 1) ||
    (score.alignment_accuracy !== null && score.alignment_accuracy < 1)

  return (
    <section className="rounded border border-rule bg-paper-card p-4">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="data text-sm text-ink">{score.document_id}</span>
        <span className="data text-xs text-ink-3">
          召回 {percent(score.recall)} · 精确 {percent(score.precision)} · 对齐{' '}
          {percent(score.alignment_accuracy)}
        </span>
        <span className="data ml-auto text-xs text-ink-3">
          {score.gold_total} 条人工 / {score.predicted_total} 条预测
        </span>
      </div>

      {weak && (score.missed.length || score.spurious.length) ? (
        <div className="mt-3 grid gap-3 border-t border-rule pt-3 sm:grid-cols-2">
          {score.missed.length ? (
            <div>
              <h3 className="text-xs text-azurite">漏抽（人工标了，模型没说）</h3>
              <ul className="font-display mt-1 space-y-1 text-[13px] leading-relaxed text-ink-2">
                {score.missed.map((quote) => (
                  <li key={quote} className="border-l-2 border-azurite-soft pl-2">
                    {quote}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {score.spurious.length ? (
            <div>
              <h3 className="text-xs text-ink-3">多抽（模型说了，人工没标）</h3>
              <ul className="mt-1 space-y-1 text-[13px] leading-relaxed text-ink-2">
                {score.spurious.map((item) => (
                  <li key={item} className="border-l-2 border-rule pl-2">
                    {item}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
