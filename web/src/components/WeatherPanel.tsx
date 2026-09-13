import type { TripWeatherOut } from '@/lib/api'
import { monthDay, weekday } from '@/lib/format'

/**
 * 多城市天气。按城市分开呈现——「北京下雨」和「西安下雨」对行程安排的含义
 * 完全不同，合成一张表就看不出这个区别了。
 *
 * 天气是硬事实，来源必须标出来：Open-Meteo 给 16 天，高德只给约 4 天，
 * 用户得知道自己看到的是哪一种。
 */
export function WeatherPanel({
  weather,
  loading,
  error,
  onLoad,
}: {
  weather: TripWeatherOut | null
  loading: boolean
  error: string | null
  onLoad: () => void
}) {
  return (
    <section className="rounded border border-rule bg-paper-card p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
        <h2 className="font-display text-xl">天气</h2>
        <button
          type="button"
          onClick={onLoad}
          disabled={loading}
          className="text-sm text-azurite underline disabled:opacity-40"
        >
          {loading ? '查询中…' : weather ? '重新查询' : '查询天气'}
        </button>
      </div>

      {error ? <p className="mt-3 text-sm text-cinnabar">{error}</p> : null}

      {!weather && !loading && !error ? (
        <p className="mt-3 text-sm text-ink-3">
          旅行日期越近，预报越准。出发前两周内再来看一次最有用。
        </p>
      ) : null}

      {weather ? (
        <div className="mt-4 space-y-4">
          {weather.cities.map((city) => (
            <div key={city.city_name}>
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-display text-base">{city.city_name}</span>
                {/* 没取到预报时**不显示来源**：`source_label` 为 null。
                    原先这里是「来源 高德」配一片空白——高德其实什么都没返回，
                    那句话会让人以为查过了、那几天就是没数据。 */}
                {city.source_label ? (
                  <span className="text-xs text-ink-3">来源 {city.source_label}</span>
                ) : null}
              </div>

              {city.days.length === 0 ? (
                <p className="mt-1 text-sm text-ink-3">{city.note ?? '这个区间没有预报'}</p>
              ) : (
                <ul className="mt-1.5 m-0 list-none space-y-0.5 p-0">
                  {city.days.map((day) => (
                    <li key={day.date} className="flex flex-wrap items-baseline gap-x-3 text-sm">
                      <span className="data w-12 shrink-0 text-ink-2">{monthDay(day.date)}</span>
                      <span className="w-6 shrink-0 text-xs text-ink-3">{weekday(day.date).slice(1)}</span>
                      <span className="w-20 shrink-0">{day.text}</span>
                      <span className="data w-20 shrink-0 text-ink-2">{day.temperature_text}</span>
                      {day.precipitation_probability !== null &&
                      day.precipitation_probability > 0 ? (
                        <span className="data text-xs text-ink-3">
                          降水 {Math.round(day.precipitation_probability)}%
                        </span>
                      ) : null}
                      {day.is_bad_outdoor ? (
                        <span className="rounded-sm bg-cinnabar-soft px-1.5 py-0.5 text-xs text-cinnabar">
                          不宜户外
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}

              {city.days.length > 0 && city.note ? (
                <p className="mt-1 text-xs text-ink-3">{city.note}</p>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  )
}
