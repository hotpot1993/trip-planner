import { useRef, useState } from 'react'

import { ApiError, streamPlan, type ApiErrorBody } from '@/lib/api'
import { navigate } from '@/lib/router'

/**
 * 生成行程：对话式的入口。
 *
 * 用户用一句话说清需求，引擎跑十来个节点。每个节点开始时后端推一条事件，
 * 所以这里能显示进度而不是让人对着空屏等——这是同步接口做不到的。
 *
 * 也刻意不做「一句话直接出结果」的错觉：进度是真实发生的阶段，卡在哪一步
 * 就停在哪一步。失败时同样是可读的说明，不是一句通用的错误。
 */
export function PlanCard({ onCreated }: { onCreated: () => void }) {
  const [query, setQuery] = useState('')
  const [startDate, setStartDate] = useState('')
  const [days, setDays] = useState('')
  const [running, setRunning] = useState(false)
  const [stages, setStages] = useState<{ node: string; label: string }[]>([])
  const [failure, setFailure] = useState<ApiErrorBody | null>(null)
  const controller = useRef<AbortController | null>(null)

  const start = async () => {
    const text = query.trim()
    if (!text || running) return

    setRunning(true)
    setStages([])
    setFailure(null)

    const abort = new AbortController()
    controller.current = abort

    try {
      await streamPlan(
        {
          query: text,
          ...(startDate ? { start_date: startDate } : {}),
          ...(days ? { days: Number(days) } : {}),
        },
        (event) => {
          if (event.type === 'stage') {
            setStages((prev) =>
              prev.some((s) => s.node === event.node)
                ? prev
                : [...prev, { node: event.node, label: event.label }],
            )
            return
          }
          if (event.type === 'done') {
            onCreated()
            navigate({ name: 'trip', tripId: event.tripId })
            return
          }
          setFailure(event.body)
        },
        abort.signal,
      )
    } finally {
      controller.current = null
      setRunning(false)
    }
  }

  const cancel = () => {
    controller.current?.abort()
    controller.current = null
    setRunning(false)
  }

  return (
    <section className="rounded border border-rule bg-paper-card p-5">
      <h2 className="font-display text-xl">生成行程</h2>
      <p className="mt-1 text-sm text-ink-2">
        用一句话说清想去哪、几天、什么偏好。生成过程会逐步显示，不用干等。
      </p>

      <label className="mt-4 block text-sm text-ink-2" htmlFor="plan-query">
        出行需求
      </label>
      <textarea
        id="plan-query"
        rows={3}
        className="mt-1 w-full resize-y rounded border border-rule bg-paper px-3 py-2 text-sm leading-relaxed"
        placeholder="想去南京看博物馆和老建筑，节奏别太赶，三天"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        disabled={running}
      />

      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">
        <span className="flex items-center gap-2">
          <label className="text-sm text-ink-2" htmlFor="plan-date">
            出发日期
          </label>
          <input
            id="plan-date"
            type="date"
            className="data rounded border border-rule bg-paper px-2.5 py-1.5 text-sm"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            disabled={running}
          />
        </span>
        <span className="flex items-center gap-2">
          <label className="text-sm text-ink-2" htmlFor="plan-days">
            天数
          </label>
          <input
            id="plan-days"
            type="number"
            min={1}
            max={60}
            className="data w-20 rounded border border-rule bg-paper px-2.5 py-1.5 text-sm"
            placeholder="自动"
            value={days}
            onChange={(e) => setDays(e.target.value)}
            disabled={running}
          />
        </span>
        <span className="text-xs text-ink-3">日期与天数留空也可以，引擎会从需求里读</span>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2">
        <button
          type="button"
          disabled={!query.trim() || running}
          onClick={start}
          className="rounded bg-azurite px-4 py-2 text-sm text-paper-card hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {running ? '生成中…' : '生成行程'}
        </button>
        {running ? (
          <button type="button" onClick={cancel} className="text-sm text-ink-3 underline">
            取消
          </button>
        ) : null}
      </div>

      {stages.length > 0 ? <Progress stages={stages} running={running} /> : null}
      {failure ? <Failure body={failure} /> : null}
    </section>
  )
}

function Progress({
  stages,
  running,
}: {
  stages: { node: string; label: string }[]
  running: boolean
}) {
  return (
    <ol className="mt-4 m-0 list-none space-y-1.5 p-0">
      {stages.map((stage, index) => {
        // 后端在节点「开始」时推事件，所以最后一条就是正在进行的那一步
        const current = running && index === stages.length - 1
        return (
          <li key={stage.node} className="flex items-baseline gap-2.5 text-sm">
            <span
              aria-hidden
              className={
                current
                  ? 'mt-1 inline-block size-2 shrink-0 rounded-full bg-azurite'
                  : 'mt-1 inline-block size-2 shrink-0 rounded-full bg-malachite'
              }
            />
            <span className={current ? 'text-ink' : 'text-ink-3'}>{stage.label}</span>
            {current ? <span className="text-xs text-ink-3">进行中</span> : null}
          </li>
        )
      })}
    </ol>
  )
}

function Failure({ body }: { body: ApiErrorBody }) {
  const message = body.message ?? body.detail ?? '生成失败'
  const missing = body.missing_fields ?? []
  const cities = body.city_names ?? []
  const keys = body.missing_keys ?? []

  return (
    <div className="mt-4 rounded border border-cinnabar/30 bg-cinnabar-soft px-4 py-3">
      <p className="text-sm text-cinnabar">{message}</p>

      {keys.length > 0 ? (
        <>
          <p className="mt-2 text-sm text-ink-2">
            在项目根目录的 <code className="data">.env.local</code> 里填上：
          </p>
          <ul className="mt-1 m-0 list-inside list-disc p-0 text-sm text-ink-2">
            {keys.map((key) => (
              <li key={key}>
                <code className="data">{key}</code>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-ink-2">填好后需要重启本地服务才会生效。</p>
        </>
      ) : null}

      {missing.length > 0 ? (
        <>
          <p className="mt-2 text-sm text-ink-2">把下面这些补进需求描述里，再生成一次：</p>
          <ul className="mt-1 m-0 list-inside list-disc p-0 text-sm text-ink-2">
            {missing.map((field) => (
              <li key={field}>{field}</li>
            ))}
          </ul>
        </>
      ) : null}

      {cities.length > 0 ? (
        <p className="mt-2 text-xs text-ink-2">
          换一种城市写法试试，比如加上省份或写成全称。
        </p>
      ) : null}

      {body.error === 'amap_unavailable' ? (
        <p className="mt-2 text-xs text-ink-2">
          检查项目根目录的 <code className="data">.env.local</code> 里的
          <code className="data"> AMAP_API_KEY</code>，以及网络是否通。
        </p>
      ) : null}
    </div>
  )
}

/** 供其他地方复用：把 ApiError 拆成可展示的两段。 */
export function describeApiError(cause: unknown): { message: string; hint: string | null } {
  if (cause instanceof ApiError) {
    return { message: cause.message, hint: cause.hint }
  }
  return { message: cause instanceof Error ? cause.message : String(cause), hint: null }
}
