import { useEffect, useState } from 'react'

import { fetchHealth, type HealthResponse } from '@/lib/api'

/**
 * M0 阶段的启动页：证明前后端连通，并把运行状态如实摊开。
 *
 * 它刻意不是最终界面——正式的行程编辑器与对话入口属于 M1。
 * 但在 M0 它能回答一个真问题：服务、表结构、引擎、配置四段是否都就位。
 */
export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    fetchHealth()
      .then((data) => {
        if (alive) setHealth(data)
      })
      .catch((cause: unknown) => {
        if (alive) setError(cause instanceof Error ? cause.message : String(cause))
      })
    return () => {
      alive = false
    }
  }, [])

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <header className="mb-10">
        <p className="font-mono-num text-xs tracking-[0.3em] text-ink-600 uppercase">Lushu</p>
        <h1 className="mt-2 text-4xl font-semibold tracking-tight">路书</h1>
        <p className="mt-3 text-sm leading-relaxed text-ink-300">
          本地优先的旅行攻略规划工具。多城市规划是骨架，攻略提纯与景点预约是挂在它上面的信息层。
        </p>
      </header>

      <StageBanner />

      {error && (
        <section className="mt-8 rounded-lg border border-rose-450/40 bg-rose-450/10 p-5">
          <h2 className="text-sm font-medium text-rose-450">拿不到运行状态</h2>
          <p className="mt-2 text-sm text-ink-300">{error}</p>
          <p className="mt-3 text-xs text-ink-600">
            确认后端已启动：在项目根目录运行
            <code className="font-mono-num ml-1 text-ink-300">python -m lushu</code>
          </p>
        </section>
      )}

      {!health && !error && <p className="mt-8 text-sm text-ink-600">正在读取运行状态…</p>}

      {health && (
        <div className="mt-8 space-y-3">
          <StatusCard
            title="服务"
            tone="ok"
            rows={[
              ['版本', health.version],
              ['库文件', health.database.path],
            ]}
          />
          <StatusCard
            title="表结构"
            tone={health.schema_version >= 1 ? 'ok' : 'bad'}
            rows={[
              ['版本', String(health.schema_version)],
              ['库文件存在', health.database.exists ? '是' : '否'],
            ]}
          />
          <StatusCard
            title="引擎"
            tone={health.engine.available ? 'ok' : 'bad'}
            rows={[
              ['可用', health.engine.available ? '是' : '否'],
              ['上游提交', health.engine.upstream_commit ?? '未知'],
              ['模块数', String(health.engine.modules.length)],
              ...(health.engine.error ? ([['错误', health.engine.error]] as [string, string][]) : []),
            ]}
          />
          {health.warnings.length > 0 && (
            <StatusCard
              title="配置"
              tone="warn"
              rows={health.warnings.map((w) => ['缺失', w] as [string, string])}
              footnote="缺 Key 只影响依赖外部服务的功能，服务本身照常运行。"
            />
          )}
        </div>
      )}

      <footer className="mt-12 border-t border-ink-800 pt-5 text-xs leading-relaxed text-ink-600">
        当前处于 M0 底座阶段。行程编辑器、对话入口、攻略提纯与预约子系统属于后续阶段，
        进度见项目根目录的 README。
      </footer>
    </main>
  )
}

function StageBanner() {
  return (
    <section className="rounded-lg border border-amber-450/30 bg-amber-450/5 px-5 py-4">
      <p className="text-sm text-amber-450">M0 · 底座</p>
      <p className="mt-1 text-xs text-ink-300">
        仓库结构、引擎隔离、表结构、前后端骨架与架构边界测试。
      </p>
    </section>
  )
}

type Tone = 'ok' | 'warn' | 'bad'

const TONE_DOT: Record<Tone, string> = {
  ok: 'bg-jade-500',
  warn: 'bg-amber-450',
  bad: 'bg-rose-450',
}

function StatusCard({
  title,
  tone,
  rows,
  footnote,
}: {
  title: string
  tone: Tone
  rows: [string, string][]
  footnote?: string
}) {
  return (
    <section className="rounded-lg border border-ink-800 bg-ink-900 p-5">
      <h2 className="flex items-center gap-2 text-sm font-medium">
        <span className={`inline-block size-2 rounded-full ${TONE_DOT[tone]}`} aria-hidden />
        {title}
      </h2>
      <dl className="mt-3 space-y-1.5">
        {rows.map(([label, value]) => (
          <div key={`${label}-${value}`} className="flex gap-4 text-sm">
            <dt className="w-24 shrink-0 text-ink-600">{label}</dt>
            <dd className="font-mono-num min-w-0 break-all text-ink-300">{value}</dd>
          </div>
        ))}
      </dl>
      {footnote && <p className="mt-3 text-xs text-ink-600">{footnote}</p>}
    </section>
  )
}
