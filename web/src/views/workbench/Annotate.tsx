import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  addGoldLabel,
  deleteGoldLabel,
  getGoldContext,
  listGoldDocuments,
  markGoldDone,
  patchGoldLabel,
  searchLocalPois,
  type GoldCandidateOut,
  type GoldContextOut,
  type GoldDocumentOut,
  type GoldLabelOut,
  type LocalPoiOut,
  type Polarity,
} from '@/lib/api'
import { layersOf, segmentBody, selectionRange, type MarkSpan } from '@/lib/marks'

/**
 * 金标准标注台。
 *
 * 这一页要让人**标得完 30 篇**，所以形状是校勘而不是表单：
 * 左边是正文，右边是批注栏，中间的媒介是「用鼠标圈一句话」。
 *
 * 两条决定这一页长什么样的取舍：
 *
 * 1. **正文上用鼠标划一段，就得到一条待标注的引文**，不用从正文里复制粘贴。
 *    偏移由程序从 DOM 选区反算，人只负责判断与命名。
 * 2. **模型抽出的候选默认不显示。** 看过模型输出再标，人会不自觉地只修
 *    模型给的东西而漏掉模型没抽到的——这是金标准最常见的偏差来源。
 *    所以它藏在一个开关后面，打开时会记下来（`model_output_seen`），
 *    这条记录跟着这篇素材进库，日后判断一个偏乐观的指标是不是标注方式造成的。
 */

interface Selection {
  start: number
  end: number
  text: string
}

export function Annotate() {
  const [documents, setDocuments] = useState<GoldDocumentOut[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [context, setContext] = useState<GoldContextOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [showMachine, setShowMachine] = useState(false)
  const [sawMachine, setSawMachine] = useState(false)
  const [flash, setFlash] = useState<string | null>(null)

  const loadDocuments = useCallback(async () => {
    try {
      const rows = await listGoldDocuments()
      setDocuments(rows)
      setActiveId((current) => current ?? rows[0]?.document_id ?? null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [])

  useEffect(() => {
    void loadDocuments()
  }, [loadDocuments])

  const loadContext = useCallback(async (documentId: string) => {
    try {
      setContext(await getGoldContext(documentId))
      setSelection(null)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [])

  useEffect(() => {
    if (activeId) void loadContext(activeId)
  }, [activeId, loadContext])

  // 换一篇素材就把开关合上：它是「这一篇标的时候看过没有」，不是全局偏好
  useEffect(() => {
    setShowMachine(false)
    setSawMachine(false)
  }, [activeId])

  const spans = useMemo(() => buildSpans(context, showMachine), [context, showMachine])

  const addLabel = async (polarity: Polarity, subjectName: string, poiId: string | null) => {
    if (!activeId || !selection) return
    setBusy(true)
    try {
      await addGoldLabel(activeId, {
        quote: selection.text,
        polarity,
        subject_name: subjectName.trim() || null,
        expected_poi_id: poiId,
      })
      window.getSelection()?.removeAllRanges()
      setSelection(null)
      await loadContext(activeId)
      await loadDocuments()
      setFlash(polarity === 'avoid' ? '已记为避坑' : '已记为打卡')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const removeLabel = async (labelId: string) => {
    setBusy(true)
    try {
      await deleteGoldLabel(labelId)
      if (activeId) await loadContext(activeId)
      await loadDocuments()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const attachPoi = async (labelId: string, poiId: string | null) => {
    setBusy(true)
    try {
      await patchGoldLabel(labelId, { expected_poi_id: poiId, set_poi: true })
      if (activeId) await loadContext(activeId)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const toggleDone = async (done: boolean) => {
    if (!activeId) return
    setBusy(true)
    try {
      await markGoldDone(activeId, { done, model_output_seen: sawMachine })
      await loadContext(activeId)
      await loadDocuments()
      setFlash(done ? '已计入金标准集' : '已移出金标准集')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    if (!flash) return
    const timer = window.setTimeout(() => setFlash(null), 2400)
    return () => window.clearTimeout(timer)
  }, [flash])

  const done = documents.filter((item) => item.in_gold_set).length

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div>
          <h1 className="font-display text-2xl">金标准标注</h1>
          <p className="mt-1 text-sm text-ink-2">
            读原文，把里面<strong className="font-normal text-ink">真正有用</strong>
            的结论圈出来。位置由程序算，你不用管偏移。
          </p>
        </div>
        <Progress done={done} total={documents.length} />
      </header>

      {error ? (
        <p className="rounded border border-cinnabar/30 bg-cinnabar-soft px-4 py-3 text-sm text-cinnabar">
          {error}
        </p>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-4">
          <DocumentPicker
            documents={documents}
            activeId={activeId}
            onPick={setActiveId}
          />
          {context ? (
            <article
              className="rounded border border-rule bg-paper-card p-5"
              onMouseUp={() => {
                const picked = selectionRange(window.getSelection(), context.body)
                setSelection(picked)
              }}
            >
              <div className="mb-3 flex flex-wrap items-baseline justify-between gap-3 border-b border-rule pb-3">
                <span className="font-display text-sm text-ink-2">
                  {context.title ?? '（无标题）'}
                </span>
                <span className="data text-xs text-ink-3">
                  {context.body.length} 字 · {context.labels.length} 条标注
                </span>
              </div>
              <Body
                body={context.body}
                spans={spans}
                onPick={(start, end) =>
                  setSelection({ start, end, text: context.body.slice(start, end) })
                }
              />
            </article>
          ) : (
            <p className="text-sm text-ink-3">读取中…</p>
          )}
        </div>

        <aside className="min-w-0 space-y-4">
          <MachineToggle
            on={showMachine}
            count={context?.candidates.length ?? 0}
            onChange={(next) => {
              setShowMachine(next)
              if (next) setSawMachine(true)
            }}
          />
          {selection ? (
            <LabelForm
              selection={selection}
              busy={busy}
              machineHint={hintFor(selection, context, showMachine)}
              onCancel={() => {
                window.getSelection()?.removeAllRanges()
                setSelection(null)
              }}
              onSave={addLabel}
            />
          ) : (
            <p className="rounded border border-dashed border-rule px-4 py-3 text-sm text-ink-3">
              在左边的正文里划一句话，就能把它标成避坑或打卡。
            </p>
          )}
          <LabelList
            labels={context?.labels ?? []}
            busy={busy}
            onRemove={removeLabel}
            onAttachPoi={attachPoi}
          />
          {context ? (
            <DonePanel
              context={context}
              sawMachine={sawMachine}
              busy={busy}
              onToggle={toggleDone}
            />
          ) : null}
        </aside>
      </div>

      {flash ? (
        <p
          role="status"
          className="fixed bottom-6 left-1/2 -translate-x-1/2 rounded border border-malachite/40 bg-malachite-soft px-4 py-2 text-sm text-ink"
        >
          {flash}
        </p>
      ) : null}
    </div>
  )
}

// ─── 正文 ────────────────────────────────────────────────────

function Body({
  body,
  spans,
  onPick,
}: {
  body: string
  spans: MarkSpan[]
  onPick: (start: number, end: number) => void
}) {
  const segments = useMemo(() => segmentBody(body, spans), [body, spans])
  const byId = useMemo(() => new Map(spans.map((span) => [span.id, span])), [spans])

  return (
    <div className="font-display text-[15px] leading-[2] whitespace-pre-wrap text-ink">
      {segments.map((segment) => {
        const { human, machine } = layersOf(segment)
        const classes = [
          human ? (human.polarity === 'avoid' ? 'mark-avoid' : 'mark-highlight') : '',
          // 机器那层刻意更弱：虚线 + 不加底色。它不是结论，只是一个待核对的提议
          machine ? 'mark-machine' : '',
        ]
          .filter(Boolean)
          .join(' ')

        if (!human && !machine) {
          return (
            <span key={segment.start} data-start={segment.start}>
              {segment.text}
            </span>
          )
        }

        if (!human) {
          return (
            <span
              key={segment.start}
              data-start={segment.start}
              className={classes}
              title={candidateTitle(segment.spans)}
            >
              {segment.text}
            </span>
          )
        }

        const index = markIndex(byId, human)
        return (
          <mark
            key={segment.start}
            data-start={segment.start}
            // 序号走 CSS 伪元素，**不能**放成子节点：`Range.toString()` 会把
            // 子节点的文字算进选区，于是跨过已标注段落时选出来的字符串比原文
            // 多出几个数字，偏移校验失败，划选就此失效。
            // 伪元素的内容不进 DOM 文本，选区因此仍然逐字等于原文。
            data-note={segment.end === human.end && index > 0 ? index : undefined}
            className={`${classes} cursor-pointer rounded-[2px]`}
            title={human.polarity === 'avoid' ? '已标：避坑' : '已标：打卡'}
            onClick={() => onPick(human.start, human.end)}
          >
            {segment.text}
          </mark>
        )
      })}
    </div>
  )
}

function markIndex(byId: Map<string, MarkSpan>, mark: MarkSpan): number {
  const ordered = [...byId.values()].filter((span) => span.layer === 'human')
  return ordered.findIndex((span) => span.id === mark.id) + 1
}

function candidateTitle(spans: MarkSpan[]): string {
  const machine = spans.filter((span) => span.layer === 'machine')
  return machine.length ? `模型候选 ${machine.length} 条` : ''
}

// ─── 批注栏 ──────────────────────────────────────────────────

function Progress({ done, total }: { done: number; total: number }) {
  const target = 30
  return (
    <div className="text-right">
      <p className="data text-2xl text-ink">
        {done}
        <span className="text-base text-ink-3">/{total} 篇</span>
      </p>
      <p className="mt-0.5 text-xs text-ink-3">
        {done >= target ? '已达设计要求的 30 篇' : `设计要 30 篇，还差 ${target - done} 篇`}
      </p>
    </div>
  )
}

function DocumentPicker({
  documents,
  activeId,
  onPick,
}: {
  documents: GoldDocumentOut[]
  activeId: string | null
  onPick: (id: string) => void
}) {
  if (!documents.length) {
    return (
      <p className="rounded border border-dashed border-rule px-4 py-3 text-sm text-ink-3">
        库里还没有素材。切到「导入」粘贴一篇攻略进来。
      </p>
    )
  }
  return (
    <div className="flex min-w-0 gap-2 overflow-x-auto pb-1">
      {documents.map((item) => {
        const active = item.document_id === activeId
        return (
          <button
            key={item.document_id}
            type="button"
            onClick={() => onPick(item.document_id)}
            className={[
              'shrink-0 rounded border px-3 py-2 text-left text-xs transition-colors',
              active
                ? 'border-azurite bg-azurite-soft text-ink'
                : 'border-rule bg-paper-card text-ink-2 hover:border-azurite/50',
            ].join(' ')}
          >
            <span className="block max-w-44 truncate font-display text-sm">
              {item.title ?? item.document_id}
            </span>
            <span className="data mt-0.5 block text-[11px] text-ink-3">
              {item.in_gold_set ? '已标' : '未标'} · 标注 {item.labeled} · 候选{' '}
              {item.prediction_count}
            </span>
            {/* 素材 id 也印出来：CLI 与评测报告里说的都是这个 id，
                界面只给标题的话，两边对不上号 */}
            <span className="data mt-0.5 block text-[10px] text-ink-3/80">
              {item.document_id}
            </span>
          </button>
        )
      })}
    </div>
  )
}

function MachineToggle({
  on,
  count,
  onChange,
}: {
  on: boolean
  count: number
  onChange: (next: boolean) => void
}) {
  return (
    <section
      className={[
        'rounded border px-4 py-3',
        on ? 'border-ink-3/40 bg-paper-sunk' : 'border-rule bg-paper-card',
      ].join(' ')}
    >
      <label className="flex cursor-pointer items-start gap-3 text-sm">
        <input
          type="checkbox"
          checked={on}
          onChange={(event) => onChange(event.target.checked)}
          className="mt-0.5"
        />
        <span>
          <span className="block text-ink">对照模型抽出的 {count} 条候选</span>
          <span className="mt-1 block text-xs leading-relaxed text-ink-3">
            默认不显示。先自己读完再对照，标出来的才可信；一旦显示，这篇素材会被记下
            「标注时看过模型输出」，日后指标偏乐观时这是唯一的线索。
          </span>
        </span>
      </label>
      {on ? (
        <p className="mt-2 border-t border-rule pt-2 text-xs text-ink-3">
          虚线是模型说的，不是原文里的话。
        </p>
      ) : null}
    </section>
  )
}

function hintFor(
  selection: Selection,
  context: GoldContextOut | null,
  showMachine: boolean,
): GoldCandidateOut | null {
  if (!showMachine || !context) return null
  return (
    context.candidates.find(
      (item) =>
        item.char_start !== null &&
        item.char_end !== null &&
        item.char_start < selection.end &&
        selection.start < item.char_end,
    ) ?? null
  )
}

function LabelForm({
  selection,
  busy,
  machineHint,
  onCancel,
  onSave,
}: {
  selection: Selection
  busy: boolean
  machineHint: GoldCandidateOut | null
  onCancel: () => void
  onSave: (polarity: Polarity, subjectName: string, poiId: string | null) => void
}) {
  const [polarity, setPolarity] = useState<Polarity>('avoid')
  const [subject, setSubject] = useState('')
  const [poiId, setPoiId] = useState<string | null>(null)

  useEffect(() => {
    setPolarity(machineHint?.polarity ?? 'avoid')
    setSubject(machineHint?.subject_name ?? '')
    setPoiId(null)
  }, [selection.start, selection.end, machineHint])

  return (
    <section className="rounded border border-azurite bg-paper-card p-4">
      <h2 className="text-sm text-ink">标这一段</h2>
      <blockquote className="font-display mt-2 max-h-28 overflow-y-auto border-l-2 border-azurite pl-3 text-sm leading-relaxed text-ink-2">
        {selection.text}
      </blockquote>

      <div className="mt-3 flex gap-2">
        {(
          [
            ['avoid', '避坑'],
            ['highlight', '打卡'],
          ] as [Polarity, string][]
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => setPolarity(value)}
            aria-pressed={polarity === value}
            className={[
              'flex-1 rounded border px-3 py-1.5 text-sm transition-colors',
              polarity === value
                ? value === 'avoid'
                  ? 'border-azurite bg-azurite-soft text-ink'
                  : 'border-malachite bg-malachite-soft text-ink'
                : 'border-rule text-ink-2 hover:border-ink-3',
            ].join(' ')}
          >
            {label}
          </button>
        ))}
      </div>

      <label className="mt-3 block text-xs text-ink-3">
        说的是哪个地方
        <input
          value={subject}
          onChange={(event) => setSubject(event.target.value)}
          placeholder="原文里的叫法，如 故宫、兵马俑"
          className="mt-1 w-full rounded border border-rule bg-paper px-2 py-1.5 text-sm text-ink"
        />
      </label>

      <PoiPicker value={poiId} subject={subject} onChange={setPoiId} />

      <div className="mt-3 flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => onSave(polarity, subject, poiId)}
          className="flex-1 rounded bg-azurite px-3 py-1.5 text-sm text-paper-card disabled:opacity-50"
        >
          记下这条
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded border border-rule px-3 py-1.5 text-sm text-ink-2"
        >
          取消
        </button>
      </div>
    </section>
  )
}

/**
 * 挂到哪个景点。
 *
 * 候选只来自**库里已有的** POI：金标准要衡量的是「管线能不能把它挂对」，
 * 填一个管线根本产不出的实体，对齐准确率就永远够不着。
 */
function PoiPicker({
  value,
  subject,
  onChange,
}: {
  value: string | null
  subject: string
  onChange: (poiId: string | null) => void
}) {
  const [query, setQuery] = useState('')
  const [rows, setRows] = useState<LocalPoiOut[]>([])
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const term = query || subject
    if (!term.trim()) {
      setRows([])
      return
    }
    let alive = true
    const timer = window.setTimeout(() => {
      searchLocalPois(term)
        .then((data) => {
          if (alive) setRows(data)
        })
        .catch(() => {
          if (alive) setRows([])
        })
    }, 180)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [query, subject])

  const picked = rows.find((row) => row.poi_id === value)

  return (
    <div className="mt-3">
      <div className="flex items-baseline justify-between">
        <span className="text-xs text-ink-3">对齐目标（可留空，之后在下面补）</span>
        {value ? (
          <button
            type="button"
            onClick={() => onChange(null)}
            className="text-xs text-ink-3 underline"
          >
            清空
          </button>
        ) : null}
      </div>

      <input
        value={query}
        onChange={(event) => {
          setQuery(event.target.value)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        placeholder={subject ? `按「${subject}」查库里的景点` : '输入景点名查库里已有的'}
        className="data mt-1 w-full rounded border border-rule bg-paper px-2 py-1.5 text-xs text-ink"
      />

      {value ? (
        <p className="data mt-1 text-[11px] text-malachite">
          → {picked?.label ?? value}
        </p>
      ) : null}

      {open && rows.length ? (
        <ul className="mt-1 max-h-40 overflow-y-auto rounded border border-rule bg-paper">
          {rows.map((row) => (
            <li key={row.poi_id}>
              <button
                type="button"
                onClick={() => {
                  onChange(row.poi_id)
                  setOpen(false)
                }}
                className="block w-full px-2 py-1.5 text-left text-xs hover:bg-azurite-soft"
              >
                <span className="text-ink">{row.label}</span>
                <span className="data ml-2 text-[10px] text-ink-3">
                  {row.is_root ? '本体' : '子点'} {row.poi_id}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {open && !rows.length && (query || subject).trim() ? (
        <p className="mt-1 text-[11px] text-ink-3">
          库里没有这个名字的景点。可以先留空——跑过对齐之后它就进库了。
        </p>
      ) : null}
    </div>
  )
}

function LabelList({
  labels,
  busy,
  onRemove,
  onAttachPoi,
}: {
  labels: GoldLabelOut[]
  busy: boolean
  onRemove: (labelId: string) => void
  onAttachPoi: (labelId: string, poiId: string | null) => void
}) {
  if (!labels.length) return null
  return (
    <section className="rounded border border-rule bg-paper-card p-4">
      <h2 className="text-sm text-ink">已标 {labels.length} 条</h2>
      <ol className="mt-2 space-y-2">
        {labels.map((label, index) => (
          <li key={label.label_id} className="border-t border-rule pt-2 first:border-0 first:pt-0">
            <div className="flex items-baseline gap-2 text-xs">
              <span className="data text-ink-3">{index + 1}</span>
              <span
                className={
                  label.polarity === 'avoid' ? 'text-azurite' : 'text-malachite'
                }
              >
                {label.polarity === 'avoid' ? '避坑' : '打卡'}
              </span>
              <span className="min-w-0 flex-1 truncate text-ink">
                {label.subject_name ?? '（未写主体）'}
              </span>
              <button
                type="button"
                disabled={busy}
                onClick={() => onRemove(label.label_id)}
                className="text-ink-3 hover:text-cinnabar"
                title="删掉这条标注"
              >
                删
              </button>
            </div>
            <p className="font-display mt-1 text-[13px] leading-relaxed text-ink-2">
              {label.quote}
            </p>
            <PoiAttach label={label} onChange={onAttachPoi} />
          </li>
        ))}
      </ol>
    </section>
  )
}

function PoiAttach({
  label,
  onChange,
}: {
  label: GoldLabelOut
  onChange: (labelId: string, poiId: string | null) => void
}) {
  const [editing, setEditing] = useState(false)
  const [query, setQuery] = useState('')
  const [rows, setRows] = useState<LocalPoiOut[]>([])

  useEffect(() => {
    if (!editing || !query.trim()) {
      setRows([])
      return
    }
    let alive = true
    const timer = window.setTimeout(() => {
      searchLocalPois(query)
        .then((data) => {
          if (alive) setRows(data)
        })
        .catch(() => {
          if (alive) setRows([])
        })
    }, 180)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [editing, query])

  if (!editing) {
    return (
      <p className="data mt-1 flex items-baseline gap-2 text-[11px]">
        {label.expected_poi_id ? (
          <span className="text-malachite">{label.expected_poi_id}</span>
        ) : (
          <span className="text-ink-3">没填对齐目标</span>
        )}
        <button
          type="button"
          onClick={() => {
            setEditing(true)
            setQuery(label.subject_name ?? '')
          }}
          className="text-ink-3 underline"
        >
          {label.expected_poi_id ? '改' : '补上'}
        </button>
      </p>
    )
  }

  return (
    <div className="mt-1">
      <input
        autoFocus
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="查库里已有的景点"
        className="data w-full rounded border border-rule bg-paper px-2 py-1 text-[11px]"
      />
      <ul className="mt-1 max-h-32 overflow-y-auto rounded border border-rule bg-paper">
        {rows.map((row) => (
          <li key={row.poi_id}>
            <button
              type="button"
              onClick={() => {
                onChange(label.label_id, row.poi_id)
                setEditing(false)
              }}
              className="block w-full px-2 py-1 text-left text-[11px] hover:bg-azurite-soft"
            >
              {row.label}
              <span className="data ml-2 text-[10px] text-ink-3">
                {row.is_root ? '本体' : '子点'}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <p className="mt-1 flex gap-3 text-[11px] text-ink-3">
        <button
          type="button"
          onClick={() => {
            onChange(label.label_id, null)
            setEditing(false)
          }}
          className="underline"
        >
          清空
        </button>
        <button type="button" onClick={() => setEditing(false)} className="underline">
          取消
        </button>
      </p>
    </div>
  )
}

function DonePanel({
  context,
  sawMachine,
  busy,
  onToggle,
}: {
  context: GoldContextOut
  sawMachine: boolean
  busy: boolean
  onToggle: (done: boolean) => void
}) {
  return (
    <section className="rounded border border-rule bg-paper-card p-4">
      <h2 className="text-sm text-ink">
        {context.in_gold_set ? '这篇已计入金标准集' : '这篇还没标完'}
      </h2>
      <p className="mt-1 text-xs leading-relaxed text-ink-3">
        「一条真结论都没有」也是有效结果——那样的素材进召回率的分母，
        所以标完了就要说一声，不能靠有没有标注条目来反推。
      </p>
      {sawMachine ? (
        <p className="mt-2 text-xs text-ink-2">
          本次标注过程中你打开过模型候选，这一条会记进库里。
        </p>
      ) : null}
      <button
        type="button"
        disabled={busy}
        onClick={() => onToggle(!context.in_gold_set)}
        className={[
          'mt-3 w-full rounded px-3 py-1.5 text-sm disabled:opacity-50',
          context.in_gold_set
            ? 'border border-rule text-ink-2'
            : 'bg-malachite text-paper-card',
        ].join(' ')}
      >
        {context.in_gold_set ? '移出金标准集' : '标完了，计入金标准集'}
      </button>
    </section>
  )
}

function buildSpans(context: GoldContextOut | null, showMachine: boolean): MarkSpan[] {
  if (!context) return []
  const spans: MarkSpan[] = context.labels
    .filter((label) => label.char_start !== null && label.char_end !== null)
    .map((label) => ({
      id: label.label_id,
      start: label.char_start as number,
      end: label.char_end as number,
      layer: 'human' as const,
      polarity: label.polarity,
    }))
  if (showMachine) {
    context.candidates.forEach((candidate, index) => {
      if (candidate.char_start === null || candidate.char_end === null) return
      spans.push({
        id: `machine-${index}`,
        start: candidate.char_start,
        end: candidate.char_end,
        layer: 'machine',
        polarity: candidate.polarity,
      })
    })
  }
  return spans
}
