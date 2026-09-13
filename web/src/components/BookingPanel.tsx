import type { BookingAlertOut, TripBookingOut, Urgency } from '@/lib/api'

/**
 * 预约清单。
 *
 * 这一块排在行程前面，不是随手放的：**预约的要害是时点，而时点比行程本身更
 * 紧急**。用户打开这份行程时真正可能已经晚了的事，是某个景点的票三天前就放过了。
 *
 * 视觉上有一条硬约束（设计里定的）：朱砂只标「会让你的计划落空」的信息，
 * 而且只有三类情形。这里用的是其中一类——**需要预约而没约**。
 * 所以只有「放票日已过」才配拿到朱砂；「今天放票」用石青（结构色，
 * 表示需要你注意），其余一律弱化。朱砂一旦到处用，它在真正该报警的地方
 * 就不再是信号了。
 *
 * 另有一条诚实性要求：**「清单里没有」与「不用预约」是两回事**。
 * 规则是草案状态时提醒会被跳过（Q10 的硬门禁）；规则库里压根没有这个景点时
 * 我们也不知道它要不要预约。两种都要说出来，不能让空清单冒充「不用担心」。
 */
/**
 * 预约清单。
 *
 * 数据由行程页取好传进来（它同时要把预约标到天项上）。
 * 原先这里自己取，于是同一份清单有两处各自请求、各自缓存——
 * 两边的状态迟早会不一致。
 */
export function BookingPanel({
  data,
  error,
}: {
  data: TripBookingOut | null
  error: string | null
}) {

  if (error) {
    return (
      <section className="rounded border border-rule bg-paper-card p-5">
        <h2 className="font-display text-xl">预约</h2>
        <p className="mt-2 text-sm text-ink-3">拿不到预约清单：{error}</p>
      </section>
    )
  }

  if (!data) {
    return (
      <section className="rounded border border-rule bg-paper-card p-5">
        <h2 className="font-display text-xl">预约</h2>
        <p className="mt-2 text-sm text-ink-3">读取中…</p>
      </section>
    )
  }

  const urgent = data.alerts.filter((alert) => alert.urgency === 'overdue')

  return (
    <section className="rounded border border-rule bg-paper-card p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <h2 className="font-display text-xl">预约</h2>
      </div>

      {urgent.length ? (
        <p className="mt-3 rounded border border-cinnabar/40 bg-cinnabar-soft px-4 py-3 text-sm text-ink">
          {urgent.map((alert) => alert.poi_name).join('、')} 的放票日已经过了。
          现在就去官方渠道确认还有没有票——这类景点的票过了点基本就没了。
        </p>
      ) : null}

      {data.alerts.length ? (
        <>
          <p className="mt-1 mb-4 text-sm text-ink-2">
            按放票日倒排。**提醒只在你打开这份行程时看得到**——它不会主动找你，
            所以到了该抢票的那几天，记得回来一趟。
          </p>
          <ol className="space-y-3">
            {data.alerts.map((alert) => (
              <AlertRow key={`${alert.poi_id}-${alert.visit_date}`} alert={alert} />
            ))}
          </ol>
        </>
      ) : (
        <EmptyBody pendingReview={data.pending_review} />
      )}

      {data.alerts.length && data.pending_review.length ? (
        <p className="mt-4 border-t border-rule pt-3 text-xs leading-relaxed text-ink-3">
          {data.pending_review.join('、')} 的预约规则还没复核，所以这里没有它们的提醒。
          未复核的规则不上清单——规则错一个字段，人就会白跑一趟。
        </p>
      ) : null}
    </section>
  )
}

function EmptyBody({ pendingReview }: { pendingReview: string[] }) {
  if (pendingReview.length) {
    return (
      <p className="mt-3 rounded border border-azurite bg-azurite-soft/50 px-4 py-3 text-sm text-ink-2">
        {pendingReview.join('、')} 有预约规则，但规则还没复核过，所以这里不显示提醒。
        到数据工作台把它们复核掉，清单才会有内容。
      </p>
    )
  }
  return (
    <p className="mt-3 text-sm leading-relaxed text-ink-3">
      这份行程里的景点都没有预约规则。<strong className="font-normal text-ink-2">
        这不等于它们不需要预约
      </strong>
      ——只说明规则库里还没有它们。出行前请到景点官方渠道确认一次。
    </p>
  )
}

const URGENCY_STYLE: Record<Urgency, string> = {
  // 朱砂：需要预约而没约，是设计里允许用朱砂的三类情形之一
  overdue: 'border-cinnabar/40 bg-cinnabar-soft',
  today: 'border-azurite bg-azurite-soft',
  soon: 'border-rule bg-paper',
  later: 'border-rule bg-paper',
}

const URGENCY_TAG: Record<Urgency, string> = {
  overdue: 'text-cinnabar',
  today: 'text-azurite',
  soon: 'text-ink',
  later: 'text-ink-2',
}

function AlertRow({ alert }: { alert: BookingAlertOut }) {
  const channels = alert.channels
  return (
    <li className={`rounded border px-4 py-3 ${URGENCY_STYLE[alert.urgency]}`}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-display text-base text-ink">{alert.poi_name}</span>
        {alert.city_name ? (
          <span className="text-xs text-ink-3">{alert.city_name}</span>
        ) : null}
        <span className={`data ml-auto text-sm ${URGENCY_TAG[alert.urgency]}`}>
          {alert.headline}
        </span>
      </div>

      <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
        <div className="flex gap-2">
          <dt className="text-ink-3">计划游览</dt>
          <dd className="data text-ink-2">{alert.visit_date}</dd>
        </div>
        {alert.release_date ? (
          <div className="flex gap-2">
            <dt className="text-ink-3">放票日</dt>
            <dd className="data text-ink-2">
              {alert.release_date}
              {alert.release_time ? ` ${alert.release_time}` : '（时刻未明）'}
            </dd>
          </div>
        ) : null}
      </dl>

      {channels.length ? (
        <p className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
          <span className="text-ink-3">渠道</span>
          {channels.map((channel) =>
            channel.url ? (
              <a
                key={channel.name}
                href={channel.url}
                target="_blank"
                rel="noreferrer"
                className="text-azurite underline"
              >
                {channel.name}
              </a>
            ) : (
              <span key={channel.name} className="text-ink-2">
                {channel.name}
              </span>
            ),
          )}
        </p>
      ) : null}

      {alert.requires_real_name ? (
        <p className="mt-2 text-xs leading-relaxed text-ink-2">
          需要实名。{alert.id_required_note ?? '提前把同行人的证件信息填好，放票时直接选人。'}
        </p>
      ) : null}

      {/* 「不卖现场票」「周一闭馆」「暑期延到 21:00」这类话恰恰最容易让人白跑。
          它们原本只出现在复核界面，等于查到了却没告诉用户。 */}
      {alert.note ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-ink-3">
            还有几条要注意的
          </summary>
          <p className="mt-1 border-l-2 border-rule pl-3 text-xs leading-relaxed text-ink-2">
            {alert.note}
          </p>
        </details>
      ) : null}
    </li>
  )
}
