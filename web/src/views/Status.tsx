import { useEffect, useState } from 'react'

import { fetchHealth, type HealthResponse } from '@/lib/api'

/**
 * 运行状态。它回答一个具体问题：服务、表结构、引擎、配置四段是否都就位。
 * 缺 Key 只作为警告——服务本身照常运行，只是依赖外部接口的功能不可用。
 */
export function Status() {
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

  if (error) {
    return (
      <section className="rounded border border-cinnabar/30 bg-cinnabar-soft p-5">
        <h2 className="text-sm text-cinnabar">拿不到运行状态</h2>
        <p className="mt-2 text-sm text-ink-2">{error}</p>
        <p className="mt-3 text-xs text-ink-3">
          确认后端已启动：在项目根目录运行
          <code className="data ml-1">python -m lushu</code>
        </p>
      </section>
    )
  }

  if (!health) return <p className="text-sm text-ink-3">读取中…</p>

  return (
    <div className="max-w-2xl space-y-3">
      <h1 className="font-display text-2xl">运行状态</h1>

      <Card
        title="服务"
        tone="ok"
        rows={[
          ['版本', health.version],
          ['库文件', health.database.path],
        ]}
      />
      <Card
        title="表结构"
        tone={health.schema_version >= 1 ? 'ok' : 'bad'}
        rows={[
          ['版本', String(health.schema_version)],
          ['库文件存在', health.database.exists ? '是' : '否'],
        ]}
      />
      <Card
        title="引擎"
        tone={health.engine.available ? 'ok' : 'bad'}
        rows={[
          ['可用', health.engine.available ? '是' : '否'],
          ['上游提交', health.engine.upstream_commit ?? '未知'],
          ['模块数', String(health.engine.modules.length)],
          ...(health.engine.error ? ([['错误', health.engine.error]] as [string, string][]) : []),
        ]}
      />
      {health.warnings.length > 0 ? (
        <Card
          title="配置"
          tone="warn"
          rows={health.warnings.map((w) => ['缺失', w] as [string, string])}
          footnote="缺 Key 只影响依赖外部接口的功能。复制 .env.example 为 .env.local 后填入即可。"
        />
      ) : null}
    </div>
  )
}

type Tone = 'ok' | 'warn' | 'bad'

const TONE_DOT: Record<Tone, string> = {
  ok: 'bg-malachite',
  warn: 'bg-cinnabar',
  bad: 'bg-cinnabar',
}

function Card({
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
    <section className="rounded border border-rule bg-paper-card p-5">
      <h2 className="flex items-center gap-2 text-sm">
        <span className={`inline-block size-2 rounded-full ${TONE_DOT[tone]}`} aria-hidden />
        {title}
      </h2>
      <dl className="mt-3 space-y-1.5">
        {rows.map(([label, value]) => (
          <div key={`${label}-${value}`} className="flex gap-4 text-sm">
            <dt className="w-24 shrink-0 text-ink-3">{label}</dt>
            <dd className="data min-w-0 break-all text-ink-2">{value}</dd>
          </div>
        ))}
      </dl>
      {footnote ? <p className="mt-3 text-xs text-ink-3">{footnote}</p> : null}
    </section>
  )
}
