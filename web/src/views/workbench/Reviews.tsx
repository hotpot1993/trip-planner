import { useCallback, useEffect, useState } from 'react'

import {
  listBookingRules,
  reviewBookingRule,
  type BookingRuleOut,
} from '@/lib/api'

/**
 * 预约规则的复核队列（Q47 的第三类队列）。
 *
 * 这一页要守的是设计里那条红线（Q10）：**未经复核的规则不得展示**。
 * 预约规则错一个字段，用户就会白跑一趟——这是本项目唯一会直接伤害用户的
 * 失败模式。所以这不是一个「审核后台」，而是一道**闸门**，
 * 界面的形状要让人意识到自己在放行什么：
 *
 * 1. **体检结果和规则排在一起**，而不是等点开才看。要签的字旁边就写着风险。
 * 2. **有错的不给签**（服务端会拒），界面直接把按钮禁掉并说明原因——
 *    让人点了才知道不行是浪费他的时间。
 * 3. **来源链接是可点的**，而且默认要打开看一眼。复核的全部意义就是
 *    「我看过那个页面了」。
 *
 * 规则会变（湖南博物院 2026-07 刚从「提前 7 天」改成「提前 5 天」），
 * 所以复核之后还能**撤回**——撤回的是签字，不是证据。
 */

const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

export function Reviews() {
  const [rules, setRules] = useState<BookingRuleOut[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [pendingOnly, setPendingOnly] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const rows = await listBookingRules(pendingOnly)
      setRules(rows)
      setActiveId((current) =>
        current && rows.some((row) => row.poi_id === current)
          ? current
          : (rows[0]?.poi_id ?? null),
      )
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [pendingOnly])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!flash) return
    const timer = window.setTimeout(() => setFlash(null), 2600)
    return () => window.clearTimeout(timer)
  }, [flash])

  const active = rules.find((rule) => rule.poi_id === activeId) ?? null

  const act = async (payload: { revoke?: boolean; evidence_url?: string }) => {
    if (!active) return
    setBusy(true)
    setError(null)
    try {
      await reviewBookingRule(active.poi_id, payload)
      setFlash(payload.revoke ? '已撤回复核，不再对用户可见' : '已复核，规则对用户可见了')
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div>
          <h1 className="font-display text-2xl">预约规则复核</h1>
          <p className="mt-1 max-w-2xl text-sm text-ink-2">
            未经复核的规则不会出现在行程上。签这个字的意思是「我看过来源页面了」——
            规则错一个字段，人就会白跑一趟。
          </p>
        </div>
        <label className="flex cursor-pointer items-center gap-2 text-sm text-ink-2">
          <input
            type="checkbox"
            checked={pendingOnly}
            onChange={(event) => setPendingOnly(event.target.checked)}
          />
          只看待复核
        </label>
      </header>

      {error ? (
        <p className="rounded border border-cinnabar/30 bg-cinnabar-soft px-4 py-3 text-sm text-cinnabar">
          {error}
        </p>
      ) : null}

      {!rules.length ? (
        <p className="rounded border border-dashed border-rule px-4 py-6 text-sm leading-relaxed text-ink-3">
          {pendingOnly
            ? '没有待复核的规则了。取消上面的勾选可以看已复核的全部。'
            : '规则库是空的。跑 ls booking seed 把种子写进来。'}
        </p>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[18rem_minmax(0,1fr)]">
          <ol className="min-w-0 space-y-1.5">
            {rules.map((rule) => (
              <li key={rule.poi_id}>
                <button
                  type="button"
                  onClick={() => setActiveId(rule.poi_id)}
                  className={[
                    'w-full rounded border px-3 py-2 text-left transition-colors',
                    rule.poi_id === activeId
                      ? 'border-azurite bg-azurite-soft'
                      : 'border-rule bg-paper-card hover:border-azurite/50',
                  ].join(' ')}
                >
                  <span className="flex items-baseline gap-2">
                    <span className="font-display min-w-0 flex-1 truncate text-sm text-ink">
                      {rule.poi_name}
                    </span>
                    {rule.errors.length ? (
                      <span className="text-[11px] text-cinnabar">
                        {rule.errors.length} 处错误
                      </span>
                    ) : null}
                  </span>
                  <span className="data mt-0.5 block text-[11px] text-ink-3">
                    {rule.status === 'reviewed' ? '已复核' : '待复核'} ·{' '}
                    {rule.advance_days === null ? '放票口径未公布' : `提前 ${rule.advance_days} 天`}
                    {rule.release_time ? ` ${rule.release_time}` : ''}
                  </span>
                </button>
              </li>
            ))}
          </ol>

          {active ? (
            <RuleDetail rule={active} busy={busy} onAct={act} />
          ) : null}
        </div>
      )}

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

function RuleDetail({
  rule,
  busy,
  onAct,
}: {
  rule: BookingRuleOut
  busy: boolean
  onAct: (payload: { revoke?: boolean; evidence_url?: string }) => void
}) {
  const [evidence, setEvidence] = useState('')
  const blocked = rule.errors.length > 0
  const canSign = Boolean(rule.evidence_url || evidence.trim())

  useEffect(() => {
    setEvidence('')
  }, [rule.poi_id])

  return (
    <section className="min-w-0 space-y-4">
      <div className="rounded border border-rule bg-paper-card p-5">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="font-display text-lg">{rule.poi_name}</h2>
          <span
            className={[
              'rounded px-2 py-0.5 text-[11px]',
              rule.status === 'reviewed'
                ? 'bg-malachite-soft text-ink'
                : 'bg-paper-sunk text-ink-2',
            ].join(' ')}
          >
            {rule.status === 'reviewed' ? '已复核' : '草案'}
          </span>
          <span className="data ml-auto text-[11px] text-ink-3">{rule.poi_id}</span>
        </div>

        <dl className="mt-3 grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
          <Field
            label="是否需预约"
            value={rule.booking_required ? '需要' : '不需要'}
          />
          <Field
            label="提前天数"
            value={rule.advance_days === null ? '官方未公布' : `${rule.advance_days} 天`}
          />
          <Field
            label="放票时刻"
            value={rule.release_time ?? '官方未公布'}
          />
          <Field
            label="实名制"
            value={
              rule.requires_real_name === null
                ? '未写'
                : rule.requires_real_name
                  ? '需要'
                  : '不需要'
            }
          />
          <Field
            label="闭馆日"
            value={
              rule.closed_days_weekdays.length
                ? rule.closed_days_weekdays.map((day) => WEEKDAYS[day]).join('、')
                : '未写'
            }
          />
          <Field
            label="复验到期"
            value={rule.verify_due_at ?? '未复核，算不出'}
          />
        </dl>

        {rule.id_required_note ? (
          <p className="mt-3 border-t border-rule pt-3 text-xs leading-relaxed text-ink-2">
            <span className="text-ink-3">证件要求 </span>
            {rule.id_required_note}
          </p>
        ) : null}

        {rule.reviewer_note ? (
          <p className="mt-3 border-t border-rule pt-3 font-display text-[13px] leading-relaxed text-ink-2">
            {rule.reviewer_note}
          </p>
        ) : null}
      </div>

      <div className="rounded border border-rule bg-paper-card p-5">
        <h3 className="text-sm text-ink">预约渠道</h3>
        {rule.channels.length ? (
          <ul className="mt-2 space-y-1 text-sm">
            {rule.channels.map((channel) => (
              <li key={channel.name} className="flex flex-wrap items-baseline gap-2">
                {channel.url ? (
                  <a
                    href={channel.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-azurite underline"
                  >
                    {channel.name}
                  </a>
                ) : (
                  <span className="text-ink-2">{channel.name}</span>
                )}
                <span className="data text-[11px] text-ink-3">{channel.kind}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-cinnabar">
            没有渠道。用户知道要预约却无处可去，这条规则不该放出去。
          </p>
        )}
      </div>

      <Findings rule={rule} />

      <div className="rounded border border-rule bg-paper-card p-5">
        <h3 className="text-sm text-ink">
          {rule.status === 'reviewed' ? '撤回复核' : '复核'}
        </h3>

        {rule.status === 'draft' ? (
          <>
            <p className="mt-1 text-xs leading-relaxed text-ink-3">
              来源链接是这个字的全部依据。打开看一眼，确认页面上的说法与上面这些
              字段一致——尤其是放票时刻与提前天数。
            </p>
            {rule.evidence_url ? (
              <p className="mt-2 text-sm">
                <a
                  href={rule.evidence_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-azurite underline"
                >
                  打开来源页面
                </a>
              </p>
            ) : (
              <label className="mt-2 block text-xs text-ink-3">
                这条还没有来源链接，补一个才能复核
                <input
                  value={evidence}
                  onChange={(event) => setEvidence(event.target.value)}
                  placeholder="https://"
                  className="data mt-1 w-full rounded border border-rule bg-paper px-2 py-1.5 text-xs"
                />
              </label>
            )}
          </>
        ) : (
          <p className="mt-1 text-xs leading-relaxed text-ink-3">
            规则会变（湖南博物院 2026 年 7 月刚从「提前 7 天」改成「提前 5 天」）。
            发现不对时撤回，行程上立刻就不再提醒——撤回的是签字，证据留着。
          </p>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-3">
          {rule.status === 'draft' ? (
            <button
              type="button"
              disabled={busy || blocked || !canSign}
              onClick={() => onAct({ evidence_url: evidence.trim() || undefined })}
              className="rounded bg-malachite px-4 py-1.5 text-sm text-paper-card disabled:opacity-40"
            >
              我看过了，放行
            </button>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => onAct({ revoke: true })}
              className="rounded border border-rule px-4 py-1.5 text-sm text-ink-2 hover:border-cinnabar hover:text-cinnabar disabled:opacity-40"
            >
              撤回复核
            </button>
          )}
          {blocked ? (
            <span className="text-xs text-cinnabar">
              有 {rule.errors.length} 处错误，修好之前签不了
            </span>
          ) : null}
        </div>
      </div>
    </section>
  )
}

function Findings({ rule }: { rule: BookingRuleOut }) {
  if (!rule.errors.length && !rule.warnings.length) {
    return (
      <p className="rounded border border-malachite/40 bg-malachite-soft/60 px-4 py-3 text-sm text-ink-2">
        体检没有发现问题。
      </p>
    )
  }
  return (
    <div className="rounded border border-rule bg-paper-card p-5">
      <h3 className="text-sm text-ink">体检</h3>
      <ul className="mt-2 space-y-1.5 text-sm">
        {rule.errors.map((item) => (
          <li key={item} className="flex gap-2 text-cinnabar">
            <span aria-hidden>✗</span>
            <span>{item}</span>
          </li>
        ))}
        {rule.warnings.map((item) => (
          <li key={item} className="flex gap-2 text-ink-2">
            <span aria-hidden className="text-ink-3">
              !
            </span>
            <span>{item}</span>
          </li>
        ))}
      </ul>
      {!rule.errors.length ? (
        <p className="mt-2 text-xs leading-relaxed text-ink-3">
          没有错误，提醒项不影响放行——「官方未公布放票口径」这类是如实记录，
          不是缺陷：界面上会照实说，而不是编一个日期。
        </p>
      ) : null}
    </div>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-3">
      <dt className="w-20 shrink-0 text-ink-3">{label}</dt>
      <dd className="data min-w-0 text-ink-2">{value}</dd>
    </div>
  )
}
