import { useCallback, useEffect, useState } from 'react'

import { BudgetPanel } from '@/components/BudgetPanel'
import { BookingPanel } from '@/components/BookingPanel'
import { MealCoordsPanel } from '@/components/MealCoordsPanel'
import { RouteRail } from '@/components/RouteRail'
import { WeatherPanel } from '@/components/WeatherPanel'
import {
  ApiError,
  confirmTrip,
  deleteTrip,
  fetchPendingMeals,
  fetchTripBooking,
  fetchTripCoverage,
  fetchTripInsights,
  fetchTripWeather,
  getTrip,
  refreshTransfers,
  roadbookUrl,
  updateStays,
  type PendingMealsOut,
  type TripBookingOut,
  type TripCoverageOut,
  type TripDetailOut,
  type TripInsightsOut,
  type TripWeatherOut,
} from '@/lib/api'
import { dateRange, shortStamp } from '@/lib/format'
import { hrefFor, navigate } from '@/lib/router'

import { DayStepper } from './TripList'

export function TripDetail({ tripId }: { tripId: string }) {
  const [trip, setTrip] = useState<TripDetailOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [weather, setWeather] = useState<TripWeatherOut | null>(null)
  const [weatherLoading, setWeatherLoading] = useState(false)
  const [weatherError, setWeatherError] = useState<string | null>(null)
  const [insights, setInsights] = useState<TripInsightsOut | null>(null)
  const [coverage, setCoverage] = useState<TripCoverageOut | null>(null)
  const [booking, setBooking] = useState<TripBookingOut | null>(null)
  const [bookingError, setBookingError] = useState<string | null>(null)
  const [pendingMeals, setPendingMeals] = useState<PendingMealsOut | null>(null)
  const [pendingMealsError, setPendingMealsError] = useState<string | null>(null)

  const reload = useCallback(() => {
    setError(null)
    getTrip(tripId)
      .then(setTrip)
      .catch((cause: unknown) => {
        setTrip(null)
        setError(cause instanceof Error ? cause.message : String(cause))
      })
    // 软经验与行程分开取：它是知识库的内容，会随复核与重新对齐变化，
    // 不该让一次行程读取依赖整个知识库
    fetchTripInsights(tripId)
      .then(setInsights)
      .catch(() => setInsights(null))
    fetchTripCoverage(tripId)
      .then(setCoverage)
      .catch(() => setCoverage(null))
    // 预约清单在行程页取一次，两处用：顶部面板与每个天项上的标注。
    // 各自取的话，同一份清单会有两份状态，迟早不一致。
    fetchTripBooking(tripId)
      .then((payload) => {
        setBooking(payload)
        setBookingError(null)
      })
      .catch((cause: unknown) => {
        setBooking(null)
        setBookingError(cause instanceof Error ? cause.message : String(cause))
      })
    // 待补坐标的餐饮项。只查库，不联网——候选要打高德，等用户点开某一项再取。
    fetchPendingMeals(tripId)
      .then((payload) => {
        setPendingMeals(payload)
        setPendingMealsError(null)
      })
      .catch((cause: unknown) => {
        setPendingMeals(null)
        setPendingMealsError(cause instanceof Error ? cause.message : String(cause))
      })
  }, [tripId])

  useEffect(reload, [reload])

  const loadWeather = useCallback(() => {
    setWeatherLoading(true)
    setWeatherError(null)
    fetchTripWeather(tripId)
      .then(setWeather)
      .catch((cause: unknown) => {
        setWeatherError(cause instanceof Error ? cause.message : String(cause))
      })
      .finally(() => setWeatherLoading(false))
  }, [tripId])

  if (error) {
    return (
      <div className="space-y-4">
        <BackLink />
        <p className="rounded border border-cinnabar/30 bg-cinnabar-soft px-4 py-3 text-sm text-cinnabar">
          {error}
        </p>
      </div>
    )
  }

  if (!trip) {
    return <p className="text-sm text-ink-3">读取中…</p>
  }

  return (
    <div className="space-y-8">
      <BackLink />
      <TripHeader trip={trip} onChanged={reload} />

      {/* 预约排在行程前面：时点比行程本身更紧急。
          用户打开这份行程时真正可能已经晚了的事，是某个景点的票几天前放过了。 */}
      <BookingPanel data={booking} error={bookingError} />

      {/* 待补坐标的餐饮项排在行程前面：它们直接决定路书里那几段路说明有没有。
          没有坐标的地方，路书只能写「按名字问路」。 */}
      <MealCoordsPanel
        data={pendingMeals}
        error={pendingMealsError}
        tripId={trip.id}
        onPinned={(fresh) => {
          setPendingMeals(fresh)
          // 坐标写进了天项，行程本身也变了（路书的段路说明由它算出来）
          reload()
        }}
      />

      <section>
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
          <h2 className="font-display text-xl">行程</h2>
          {trip.stays.length > 1 ? (
            <RefreshTransfers tripId={trip.id} onDone={reload} />
          ) : null}
        </div>
        <p className="mt-1 mb-5 text-sm text-ink-2">
          实心圆点是已经排好的天，空心是还没安排的。总天数由各城市停留天数相加得出。
          城际转移画在它落到的那一天里。
        </p>
        <CoverageLine coverage={coverage} tripId={trip.id} />
        <RouteRail
          stays={trip.stays}
          transfers={trip.transfers}
          insights={insights ?? undefined}
          bookings={booking?.alerts ?? []}
        />
      </section>

      <BudgetPanel budget={trip.budget} />

      <WeatherPanel
        weather={weather}
        loading={weatherLoading}
        error={weatherError}
        onLoad={loadWeather}
      />

      <StaysEditor trip={trip} onSaved={reload} />
    </div>
  )
}

/** 重新查一遍城际车次。会真的问 12306，所以要有进行中的反馈。 */
/**
 * 这份行程里有多少地方是网友真的推荐过的。
 *
 * 设计 5.1 的封闭世界约束是「排程不得引入候选池之外的景点」，但候选池
 * **还没有接进排程的景点搜索**（那要改 vendored 的节点）。现在硬性拒绝
 * 整份行程会把每一份都毙掉，所以如实报出来——这条信息本身就是价值：
 * 一个只是地图上有的地方，没有任何人说过它值得去，也不知道要注意什么。
 *
 * 只是地图上有的那些**点名列出来**，不在每个天项旁边挂小徽章：
 * 一句话说清比让人去正文里找出是哪三个更有用。
 */
function CoverageLine({
  coverage,
  tripId,
}: {
  coverage: TripCoverageOut | null
  tripId: string
}) {
  if (!coverage || !coverage.total) return null
  const bare = coverage.items.filter((item) => !item.recommended)
  const names = bare.map((item) => item.title)

  return (
    <section className="mb-4 space-y-1.5">
      <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-ink-3">
        <span>
          行程里 {coverage.total} 个景点，
          <span className="text-malachite">{coverage.recommended} 个有网友写过</span>
          {bare.length ? `，${bare.length} 个只是地图上有` : ''}
          {coverage.unresolved ? `；另有 ${coverage.unresolved} 个还没对上实体` : ''}
        </span>
        <a
          href={roadbookUrl(tripId)}
          className="rounded border border-rule px-2 py-0.5 text-ink-2 no-underline hover:border-azurite"
        >
          导出路书（单文件，可离线打开）
        </a>
      </p>
      {names.length ? (
        <details className="text-xs text-ink-3">
          <summary className="cursor-pointer">
            哪 {names.length} 个只是地图上有？
          </summary>
          <p className="mt-1 leading-relaxed">
            {names.join('、')}。这些地方没有任何网友提过——没有打卡建议、
            没有避坑提醒，也不知道要不要预约。要去的话，出发前自己查一遍。
          </p>
        </details>
      ) : null}
    </section>
  )
}

function RefreshTransfers({ tripId, onDone }: { tripId: string; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      await refreshTransfers(tripId)
      onDone()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex items-baseline gap-x-3">
      {error ? <span className="text-xs text-cinnabar">{error}</span> : null}
      <button
        type="button"
        onClick={run}
        disabled={busy}
        className="text-sm text-azurite underline disabled:opacity-40"
      >
        {busy ? '查询车次中…' : '刷新城际车次'}
      </button>
    </div>
  )
}

function BackLink() {
  return (
    <a href={hrefFor({ name: 'trips' })} className="text-sm text-azurite underline">
      ← 全部行程
    </a>
  )
}

function TripHeader({ trip, onChanged }: { trip: TripDetailOut; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const onConfirm = async () => {
    setBusy(true)
    setError(null)
    try {
      await confirmTrip(trip.id)
      onChanged()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  const onDelete = async () => {
    if (!window.confirm(`删除「${trip.name}」？这份行程的内容会一并消失。`)) return
    setBusy(true)
    setError(null)
    try {
      await deleteTrip(trip.id)
      navigate({ name: 'trips' })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setBusy(false)
    }
  }

  return (
    <header>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
        <h1 className="font-display text-3xl leading-tight">{trip.name}</h1>
        {trip.status === 'confirmed' ? (
          <span className="rounded-sm bg-malachite-soft px-2 py-0.5 text-xs text-malachite">
            已确认
          </span>
        ) : (
          <span className="rounded-sm bg-paper-sunk px-2 py-0.5 text-xs text-ink-2">草稿</span>
        )}
      </div>

      <div className="mt-2 flex flex-wrap gap-x-4 text-sm text-ink-2">
        <span className="data">{dateRange(trip.start_date, trip.end_date)}</span>
        <span className="data">{trip.total_days} 天</span>
        <span>{trip.stays.map((s) => `${s.city_name} ${s.stay_days} 天`).join(' · ')}</span>
      </div>

      {trip.query ? (
        <p className="mt-3 border-l-2 border-rule pl-3 text-sm text-ink-3">“{trip.query}”</p>
      ) : null}

      {error ? <p className="mt-3 text-sm text-cinnabar">{error}</p> : null}

      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2">
        {trip.status === 'draft' ? (
          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className="rounded bg-azurite px-3 py-1.5 text-sm text-paper-card hover:opacity-90 disabled:opacity-40"
          >
            确认行程
          </button>
        ) : null}
        <button
          type="button"
          disabled={busy}
          onClick={onDelete}
          className="text-sm text-ink-3 underline hover:text-cinnabar disabled:opacity-40"
        >
          删除
        </button>
        <span className="data text-xs text-ink-3">{shortStamp(trip.updated_at)} 更新</span>
      </div>
    </header>
  )
}

// ─── 改城市与天数 ────────────────────────────────────────────

interface CityDraft {
  name: string
  days: number
}

function StaysEditor({ trip, onSaved }: { trip: TripDetailOut; onSaved: () => void }) {
  const [startDate, setStartDate] = useState(trip.start_date)
  const [cities, setCities] = useState<CityDraft[]>(() =>
    trip.stays.map((s) => ({ name: s.city_name, days: s.stay_days })),
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ message: string; hint: string | null } | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // 行程被外部改动（例如确认、重新生成）时把草稿同步过来
  useEffect(() => {
    setStartDate(trip.start_date)
    setCities(trip.stays.map((s) => ({ name: s.city_name, days: s.stay_days })))
  }, [trip])

  const totalDays = cities.reduce((sum, c) => sum + (Number.isFinite(c.days) ? c.days : 0), 0)
  const originalDays = trip.stays.reduce((sum, s) => sum + s.stay_days, 0)

  const dirty =
    startDate !== trip.start_date ||
    cities.length !== trip.stays.length ||
    cities.some((c, i) => {
      const stay = trip.stays[i]
      return !stay || stay.city_name !== c.name.trim() || stay.stay_days !== c.days
    })

  const save = async () => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await updateStays(trip.id, {
        start_date: startDate,
        cities: cities
          .filter((c) => c.name.trim())
          .map((c) => ({ name: c.name.trim(), days: c.days })),
      })
      setNotice('已保存。仍然存在的那些天保留了原有内容。')
      onSaved()
    } catch (cause) {
      if (cause instanceof ApiError) {
        setError({ message: cause.message, hint: cause.hint })
      } else {
        setError({ message: cause instanceof Error ? cause.message : String(cause), hint: null })
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="rounded border border-rule bg-paper-card p-5">
      <h2 className="font-display text-xl">城市与天数</h2>
      <p className="mt-1 text-sm text-ink-2">
        改天数会重新排日期。日期与城市都没变的那一天，原有内容会保留下来。
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-x-3">
        <label className="text-sm text-ink-2" htmlFor="edit-start-date">
          出发日期
        </label>
        <input
          id="edit-start-date"
          type="date"
          className="data rounded border border-rule bg-paper px-2.5 py-1.5 text-sm"
          value={startDate}
          onChange={(e) => setStartDate(e.target.value)}
        />
      </div>

      <ul className="mt-4 m-0 list-none space-y-2 p-0">
        {cities.map((city, index) => (
          <li key={index} className="flex flex-wrap items-center gap-2">
            <input
              className="w-40 rounded border border-rule bg-paper px-2.5 py-1.5 text-sm"
              value={city.name}
              onChange={(e) =>
                setCities((prev) =>
                  prev.map((c, i) => (i === index ? { ...c, name: e.target.value } : c)),
                )
              }
              aria-label={`第 ${index + 1} 座城市`}
            />
            <DayStepper
              value={city.days}
              onChange={(days) =>
                setCities((prev) => prev.map((c, i) => (i === index ? { ...c, days } : c)))
              }
              label={city.name || `第 ${index + 1} 座城市`}
            />
            {cities.length > 1 ? (
              <button
                type="button"
                className="text-xs text-ink-3 underline hover:text-cinnabar"
                onClick={() => setCities((prev) => prev.filter((_, i) => i !== index))}
              >
                移除
              </button>
            ) : null}
          </li>
        ))}
      </ul>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        <button
          type="button"
          className="text-sm text-azurite underline"
          onClick={() => setCities((prev) => [...prev, { name: '', days: 2 }])}
        >
          + 添加城市
        </button>
        <span className="data text-sm text-ink-2">
          {totalDays} 天
          {totalDays !== originalDays ? (
            <span className="ml-2 text-cinnabar">（原 {originalDays} 天）</span>
          ) : null}
        </span>
      </div>

      {error ? (
        <div className="mt-4 rounded border border-cinnabar/30 bg-cinnabar-soft px-4 py-3">
          <p className="text-sm text-cinnabar">{error.message}</p>
          {error.hint ? <p className="mt-1 text-xs text-cinnabar">{error.hint}</p> : null}
        </div>
      ) : null}

      {notice ? <p className="mt-4 text-sm text-malachite">{notice}</p> : null}

      <button
        type="button"
        disabled={busy || !dirty}
        onClick={save}
        className="mt-4 rounded bg-azurite px-4 py-2 text-sm text-paper-card hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {busy ? '保存中…' : dirty ? '保存改动' : '没有改动'}
      </button>
    </section>
  )
}
