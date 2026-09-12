import { useCallback, useEffect, useState } from 'react'

import { PlanCard, describeApiError } from '@/components/PlanCard'
import { createTrip, listTrips, type TripSummaryOut } from '@/lib/api'
import { shortStamp } from '@/lib/format'
import { hrefFor } from '@/lib/router'

const inputClass =
  'rounded border border-rule bg-paper px-2.5 py-1.5 text-sm text-ink outline-none focus-visible:border-azurite'

export function TripList() {
  const [trips, setTrips] = useState<TripSummaryOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(() => {
    setError(null)
    listTrips()
      .then(setTrips)
      .catch((cause: unknown) => {
        setTrips([])
        setError(describeApiError(cause).message)
      })
  }, [])

  useEffect(reload, [reload])

  return (
    <div className="space-y-8">
      {/* 对话式是主入口：说一句话就生成 */}
      <PlanCard onCreated={reload} />

      {/* 手工铺骨架是次要路径，折起来不抢注意力 */}
      <ManualCreate onCreated={reload} />

      <section>
        <h2 className="font-display text-xl">行程</h2>

        {error ? (
          <p className="mt-3 rounded border border-cinnabar/30 bg-cinnabar-soft px-4 py-3 text-sm text-cinnabar">
            {error}
          </p>
        ) : null}

        {trips === null ? (
          <p className="mt-3 text-sm text-ink-3">读取中…</p>
        ) : trips.length === 0 ? (
          <p className="mt-3 rounded border border-dashed border-rule px-4 py-8 text-center text-sm text-ink-3">
            还没有行程。在上面说一句想去哪，就能生成第一份。
          </p>
        ) : (
          <ul className="mt-3 m-0 list-none space-y-2 p-0">
            {trips.map((trip) => (
              <TripRow key={trip.id} trip={trip} />
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

function TripRow({ trip }: { trip: TripSummaryOut }) {
  return (
    <li>
      <a
        href={hrefFor({ name: 'trip', tripId: trip.id })}
        className="block rounded border border-rule bg-paper-card px-4 py-3 no-underline transition-colors hover:border-azurite"
      >
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="font-display text-base text-ink">{trip.name}</span>
          <span className="data text-xs text-ink-3">{trip.total_days} 天</span>
          {trip.status === 'confirmed' ? (
            <span className="rounded-sm bg-malachite-soft px-1.5 py-0.5 text-xs text-malachite">
              已确认
            </span>
          ) : null}
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-ink-3">
          <span>{trip.city_names.join(' · ') || '未设置城市'}</span>
          <span className="data">{shortStamp(trip.updated_at)} 更新</span>
        </div>
      </a>
    </li>
  )
}

// ─── 手工铺骨架 ──────────────────────────────────────────────

interface CityDraft {
  name: string
  days: number
}

/** 本地时区的今天，用来做日期输入框的默认值。 */
function todayIso(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

function ManualCreate({ onCreated }: { onCreated: () => void }) {
  const [startDate, setStartDate] = useState(todayIso)
  const [cities, setCities] = useState<CityDraft[]>([{ name: '', days: 3 }])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ message: string; hint: string | null } | null>(null)

  const totalDays = cities.reduce((sum, c) => sum + (Number.isFinite(c.days) ? c.days : 0), 0)
  const filled = cities.filter((c) => c.name.trim().length > 0)
  const canSubmit = filled.length > 0 && !busy

  const updateCity = (index: number, patch: Partial<CityDraft>) => {
    setCities((prev) => prev.map((c, i) => (i === index ? { ...c, ...patch } : c)))
  }

  const submit = async () => {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await createTrip({
        start_date: startDate,
        cities: filled.map((c) => ({ name: c.name.trim(), days: c.days })),
      })
      setCities([{ name: '', days: 3 }])
      onCreated()
    } catch (cause) {
      setError(describeApiError(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="rounded border border-rule bg-paper-card px-5 py-4">
      <summary className="cursor-pointer text-sm text-ink-2 marker:text-ink-3">
        或者手工铺一份空的行程骨架
      </summary>

      <p className="mt-3 text-sm text-ink-2">
        只填城市与天数，日期会自动接续排下去。生成会让每一天都有内容，手工铺的则留空等你自己填。
      </p>

      <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1">
        <label className="text-sm text-ink-2" htmlFor="start-date">
          出发日期
        </label>
        <input
          id="start-date"
          type="date"
          className={`${inputClass} data`}
          value={startDate}
          onChange={(e) => setStartDate(e.target.value)}
        />
      </div>

      <ul className="mt-4 m-0 list-none space-y-2 p-0">
        {cities.map((city, index) => (
          <li key={index} className="flex flex-wrap items-center gap-2">
            <input
              className={`${inputClass} w-40`}
              placeholder="城市，如 南京"
              value={city.name}
              onChange={(e) => updateCity(index, { name: e.target.value })}
              aria-label={`第 ${index + 1} 座城市`}
            />
            <DayStepper
              value={city.days}
              onChange={(days) => updateCity(index, { days })}
              label={city.name.trim() || `第 ${index + 1} 座城市`}
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
        <span className="data text-sm text-ink-2">小计 {totalDays} 天</span>
      </div>

      {error ? (
        <div className="mt-4 rounded border border-cinnabar/30 bg-cinnabar-soft px-4 py-3">
          <p className="text-sm text-cinnabar">{error.message}</p>
          {error.hint ? <p className="mt-1 text-xs text-cinnabar">{error.hint}</p> : null}
        </div>
      ) : null}

      <button
        type="button"
        disabled={!canSubmit}
        onClick={submit}
        className="mt-4 rounded bg-azurite px-4 py-2 text-sm text-paper-card transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {busy ? '创建中…' : '创建骨架'}
      </button>
    </details>
  )
}

export function DayStepper({
  value,
  onChange,
  label,
}: {
  value: number
  onChange: (days: number) => void
  label: string
}) {
  const step = (delta: number) => {
    const next = Math.min(30, Math.max(1, value + delta))
    onChange(next)
  }

  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        className="size-7 rounded border border-rule text-sm text-ink-2 hover:border-azurite disabled:opacity-40"
        onClick={() => step(-1)}
        disabled={value <= 1}
        aria-label={`${label} 减少一天`}
      >
        −
      </button>
      <span className="data w-10 text-center text-sm">{value}</span>
      <button
        type="button"
        className="size-7 rounded border border-rule text-sm text-ink-2 hover:border-azurite disabled:opacity-40"
        onClick={() => step(1)}
        disabled={value >= 30}
        aria-label={`${label} 增加一天`}
      >
        +
      </button>
    </span>
  )
}
