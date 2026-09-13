import { useCallback, useEffect, useState } from 'react'

import { hrefFor, navigate, WORKBENCH_TABS, type WorkbenchTab } from '@/lib/router'
import {
  ingestDocument,
  streamPipeline,
  workbenchStats,
  type IngestOut,
  type PipelineDonePayload,
  type PipelineEvent,
  type PipelineStatsOut,
  type PipelineStep,
} from '@/lib/api'
import { Annotate } from '@/views/workbench/Annotate'
import { Alignments } from '@/views/workbench/Alignments'
import { Evaluation } from '@/views/workbench/Evaluation'
import { Extractions } from '@/views/workbench/Extractions'

const TAB_LABELS: Record<WorkbenchTab, string> = {
  annotate: '标注',
  alignments: '待对齐',
  extractions: '提纯',
  eval: '评测',
}

/**
 * 数据工作台。
 *
 * 三类队列共用一个页面的形状（Q47）：**上面一条工具栏，左边一列待办，
 * 右边一块判断的地方**。它们的不同只在「判断什么」——这条提及指的是哪个
 * 景点、这条结论该不该立、这条引文有没有回到原文。
 *
 * 工具栏收着两件「喂数据」的事：导入素材、跑一轮管线。它们不属于任何一条
 * 队列，但都发生在队列之前，所以放在队列外面。
 */
export function Workbench({ tab }: { tab: WorkbenchTab }) {
  const [panel, setPanel] = useState<'none' | 'import' | 'run'>('none')
  const [stats, setStats] = useState<PipelineStatsOut | null>(null)
  const [nonce, setNonce] = useState(0)

  const loadStats = useCallback(async () => {
    try {
      setStats(await workbenchStats())
    } catch {
      setStats(null)
    }
  }, [])

  useEffect(() => {
    void loadStats()
  }, [loadStats, nonce])

  return (
    <div className="space-y-6">
      <header className="space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div>
            <h1 className="font-display text-3xl">数据工作台</h1>
            <p className="mt-1 text-sm text-ink-2">
              把网友的攻略提炼成有出处的结论，并量出这一步做得有多准。
            </p>
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setPanel(panel === 'import' ? 'none' : 'import')}
              aria-expanded={panel === 'import'}
              className={toolbarClass(panel === 'import')}
            >
              导入素材
            </button>
            <button
              type="button"
              onClick={() => setPanel(panel === 'run' ? 'none' : 'run')}
              aria-expanded={panel === 'run'}
              className={toolbarClass(panel === 'run')}
            >
              跑一轮管线
            </button>
          </div>
        </div>

        <Chain stats={stats} />

        {panel === 'import' ? (
          <ImportPanel
            onDone={() => {
              setNonce((value) => value + 1)
              navigate({ name: 'workbench', tab: 'annotate' })
            }}
          />
        ) : null}
        {panel === 'run' ? (
          <RunPanel
            onDone={() => {
              setNonce((value) => value + 1)
            }}
          />
        ) : null}
      </header>

      <nav className="flex min-w-0 flex-wrap gap-1 border-b border-rule" role="tablist">
        {WORKBENCH_TABS.map((item) => (
          <a
            key={item}
            href={hrefFor({ name: 'workbench', tab: item })}
            role="tab"
            aria-selected={tab === item}
            className={[
              '-mb-px border-b-2 px-4 py-2 text-sm no-underline transition-colors',
              tab === item
                ? 'border-azurite text-ink'
                : 'border-transparent text-ink-3 hover:text-ink',
            ].join(' ')}
          >
            {TAB_LABELS[item]}
            {item === 'alignments' && stats?.align_pending ? (
              <span className="data ml-2 text-xs text-azurite">{stats.align_pending}</span>
            ) : null}
          </a>
        ))}
      </nav>

      {tab === 'annotate' ? <Annotate key={nonce} /> : null}
      {tab === 'alignments' ? <Alignments key={nonce} /> : null}
      {tab === 'extractions' ? <Extractions key={nonce} /> : null}
      {tab === 'eval' ? <Evaluation key={nonce} /> : null}
    </div>
  )
}

function toolbarClass(active: boolean): string {
  return [
    'rounded border px-3 py-1.5 text-sm transition-colors',
    active
      ? 'border-azurite bg-azurite-soft text-ink'
      : 'border-rule bg-paper-card text-ink-2 hover:border-azurite',
  ].join(' ')
}

/**
 * 链路现状。
 *
 * 数字是**读数**不是成绩：它回答「现在该做哪一步」。素材为零就去导入，
 * 提纯为零就去跑管线，待对齐不为零就去处置。所以每个数字旁边写的是
 * 人的下一步，而不是它的字段名。
 */
function Chain({ stats }: { stats: PipelineStatsOut | null }) {
  if (!stats) return null
  const steps: { label: string; value: number; hint: string }[] = [
    { label: '素材', value: stats.documents, hint: stats.documents ? '已入库' : '先导入' },
    {
      label: '已提纯',
      value: stats.extract_documents,
      hint: stats.extract_documents < stats.documents ? '还有没提纯的' : '全部提纯过',
    },
    { label: '结论', value: stats.claims, hint: '已落库' },
    {
      label: '待对齐',
      value: stats.align_pending,
      hint: stats.align_pending ? '需要人来判断' : '队列是空的',
    },
    {
      label: '引文丢弃',
      value: stats.extract_dropped,
      hint: stats.extract_dropped ? '模型编过引文' : '引文全部回到原文',
    },
  ]

  return (
    <ol className="flex min-w-0 flex-wrap gap-x-8 gap-y-3 rounded border border-rule bg-paper-card px-5 py-3">
      {steps.map((step) => (
        <li key={step.label}>
          <p className="text-xs text-ink-3">{step.label}</p>
          <p className="data text-xl text-ink">{step.value}</p>
          <p className="text-[11px] text-ink-3">{step.hint}</p>
        </li>
      ))}
    </ol>
  )
}

function ImportPanel({ onDone }: { onDone: () => void }) {
  const [body, setBody] = useState('')
  const [title, setTitle] = useState('')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<IngestOut | null>(null)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    if (!body.trim()) return
    setBusy(true)
    setError(null)
    try {
      const created = await ingestDocument({
        body,
        title: title.trim() || undefined,
        url: url.trim() || undefined,
      })
      setResult(created)
      setBody('')
      setTitle('')
      setUrl('')
      onDone()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="rounded border border-rule bg-paper-card p-5">
      <h2 className="text-sm text-ink">粘贴一篇攻略</h2>
      <p className="mt-1 text-xs leading-relaxed text-ink-3">
        只抓公开内容，不碰需要登录的。全文留作本地底档，进路书的只有片段。
        从网址抓取请用命令行 <code className="data">ls ingest fetch</code>。
      </p>

      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="标题"
          className="rounded border border-rule bg-paper px-3 py-1.5 text-sm"
        />
        <input
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="原文网址（可选，用于识别站点）"
          className="data rounded border border-rule bg-paper px-3 py-1.5 text-sm"
        />
      </div>
      <textarea
        value={body}
        onChange={(event) => setBody(event.target.value)}
        rows={8}
        placeholder="把正文粘在这里"
        className="mt-2 w-full rounded border border-rule bg-paper px-3 py-2 font-display text-sm leading-relaxed"
      />

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy || !body.trim()}
          className="rounded bg-azurite px-4 py-1.5 text-sm text-paper-card disabled:opacity-50"
        >
          {busy ? '导入中…' : '导入'}
        </button>
        <span className="data text-xs text-ink-3">{body.length} 字</span>
        {error ? <span className="text-xs text-cinnabar">{error}</span> : null}
        {result ? <IngestResult result={result} /> : null}
      </div>
    </section>
  )
}

function IngestResult({ result }: { result: IngestOut }) {
  const text =
    result.duplicate === 'new'
      ? `已入库：${result.document_id}`
      : result.duplicate === 'repost'
        ? `已入库，与已有素材重合 ${Math.round((result.coverage ?? 0) * 100)}%，归到同一个来源组`
        : `正文指纹与 ${result.document_id} 相同，没有重复入库`
  return (
    <span className="text-xs text-malachite">
      {text}
      {result.duplicate === 'repost' ? '（置信度按组计数，不按篇）' : ''}
    </span>
  )
}

function RunPanel({ onDone }: { onDone: () => void }) {
  const [steps, setSteps] = useState<PipelineStep[]>(['extract', 'align', 'group', 'merge'])
  const [events, setEvents] = useState<PipelineEvent[]>([])
  const [running, setRunning] = useState(false)
  const [done, setDone] = useState<PipelineDonePayload | null>(null)

  const toggle = (step: PipelineStep) => {
    setSteps((current) =>
      current.includes(step) ? current.filter((item) => item !== step) : [...current, step],
    )
  }

  const start = async () => {
    setRunning(true)
    setEvents([])
    setDone(null)
    await streamPipeline({ steps }, (event) => setEvents((current) => [...current, event]))
    setRunning(false)
    onDone()
  }

  const last = events.at(-1)
  const payload = last?.type === 'done' ? last.payload : done

  useEffect(() => {
    if (last?.type === 'done') setDone(last.payload)
  }, [last])

  return (
    <section className="rounded border border-rule bg-paper-card p-5">
      <h2 className="text-sm text-ink">跑一轮管线</h2>
      <p className="mt-1 text-xs leading-relaxed text-ink-3">
        四步按顺序跑，顺序由服务端定死。提纯要花钱，已经提纯过的篇会自动跳过；
        报错的那几篇会照样列出来，不会闷掉。
      </p>

      <div className="mt-3 flex flex-wrap gap-2">
        {(
          [
            ['extract', '提纯'],
            ['align', '对齐'],
            ['group', '归组'],
            ['merge', '合并'],
          ] as [PipelineStep, string][]
        ).map(([value, label]) => (
          <label
            key={value}
            className="flex cursor-pointer items-center gap-2 rounded border border-rule px-3 py-1.5 text-sm"
          >
            <input
              type="checkbox"
              checked={steps.includes(value)}
              onChange={() => toggle(value)}
            />
            {label}
          </label>
        ))}
        <button
          type="button"
          onClick={() => void start()}
          disabled={running || !steps.length}
          className="rounded bg-azurite px-4 py-1.5 text-sm text-paper-card disabled:opacity-50"
        >
          {running ? '跑着呢…' : '开始'}
        </button>
      </div>

      {events.length ? (
        <ol className="data mt-3 max-h-56 space-y-1 overflow-y-auto text-xs">
          {events.map((event, index) => (
            <li key={index} className="text-ink-2">
              {describe(event)}
            </li>
          ))}
        </ol>
      ) : null}

      {payload ? <RunSummary payload={payload} /> : null}
    </section>
  )
}

function describe(event: PipelineEvent): string {
  switch (event.type) {
    case 'stage':
      return `▸ ${event.label}`
    case 'progress':
      return `　${event.label}（${event.index}/${event.total}）`
    case 'done':
      return '✓ 跑完了'
    case 'error':
      return `✗ ${event.body.message ?? event.body.detail ?? '失败'}`
  }
}

function RunSummary({ payload }: { payload: PipelineDonePayload }) {
  return (
    <div className="mt-3 space-y-1 border-t border-rule pt-3 text-sm text-ink-2">
      {payload.extract ? (
        <p>
          提纯 {payload.extract.documents} 篇（{payload.extract.cached} 篇命中缓存），
          通过校验 {payload.extract.accepted} 条，丢弃 {payload.extract.dropped} 条
        </p>
      ) : null}
      {payload.extract?.failed.length ? (
        <ul className="text-xs text-cinnabar">
          {payload.extract.failed.map((item) => (
            <li key={item.document_id}>
              {item.document_id}：{item.error}
            </li>
          ))}
        </ul>
      ) : null}
      {payload.align ? (
        <p>
          对齐 {payload.align.mentions} 条提及，落库 {payload.align.aligned} 条
          {payload.align.collapsed ? `（${payload.align.collapsed} 条由子点归并到本体）` : ''}
          ，仍待人工 {payload.align.pending} 条
        </p>
      ) : null}
      {payload.group ? <p>比对 {payload.group.compared} 对素材，合并 {payload.group.merged} 篇</p> : null}
      {payload.merge ? (
        <p>
          结论 {payload.merge.created} 条，高置信 {payload.merge.high_confidence} 条
        </p>
      ) : null}
    </div>
  )
}
