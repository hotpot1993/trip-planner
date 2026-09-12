/**
 * 后端接口的类型与调用。
 *
 * 字段名与 FastAPI 的响应逐字对应，改后端时这里必须同步改。
 * 一律用相对路径 `/api`：开发期由 Vite 转发到本地后端，生产期由后端自己托管。
 */

// ─── 系统 ────────────────────────────────────────────────────

export interface HealthResponse {
  status: string
  version: string
  schema_version: number
  database: { path: string; exists: boolean }
  engine: {
    available: boolean
    upstream_commit: string | null
    database_path: string | null
    modules: string[]
    error: string | null
  }
  warnings: string[]
}

export interface ClientConfig {
  amap_js_key: string
  amap_js_security_code: string
  missing_keys: string[]
}

// ─── 行程 ────────────────────────────────────────────────────

export type ItemKind = 'poi' | 'meal' | 'rest'

export interface ItemOut {
  kind: ItemKind
  title: string
  poi_id: string | null
  start_time: string | null
  end_time: string | null
  note: string | null
  lat_gcj02: number | null
  lng_gcj02: number | null
  address: string | null
  rating: number | null
  open_time: string | null
  photo: string | null
}

export interface DayOut {
  date: string
  seq_in_stay: number
  theme: string | null
  items: ItemOut[]
}

export interface StayOut {
  city_name: string
  city_adcode: string | null
  seq: number
  stay_days: number
  days: DayOut[]
}

export interface TransferAlternative {
  service_no: string
  from_station: string
  to_station: string
  dep_time: string
  arr_time: string
  duration_min: number | null
  price: number | null
  is_reference_price: boolean
  has_tickets: boolean
}

export interface TransferOut {
  id: string
  from_city_name: string
  to_city_name: string
  /** 全行程内从 0 开始的天序号。转移是这一天的一部分，不是独立的一天。 */
  day_index: number
  day: string
  mode: string
  service_no: string | null
  from_station: string | null
  to_station: string | null
  dep_time: string | null
  arr_time: string | null
  duration_min: number | null
  price: number | null
  price_source: string
  is_reference_price: boolean
  has_tickets: boolean | null
  advice_reason: string | null
  note: string | null
  alternatives: TransferAlternative[]
}

export interface BudgetItemOut {
  category: string
  label: string
  amount: number
  currency: string
  is_reference_price: boolean
  source: string | null
  note: string | null
}

export interface BudgetPanelOut {
  intercity: BudgetItemOut[]
  others: BudgetItemOut[]
  intercity_total: number
  other_total: number
  total: number
  has_reference_prices: boolean
}

export interface DayWeatherOut {
  date: string
  text: string
  night_text: string | null
  temp_min: number | null
  temp_max: number | null
  temperature_text: string
  precipitation_probability: number | null
  is_bad_outdoor: boolean
}

export interface CityWeatherOut {
  city_name: string
  source: string
  source_label: string
  days: DayWeatherOut[]
  note: string | null
}

export interface TripWeatherOut {
  start_date: string
  end_date: string
  cities: CityWeatherOut[]
}

export interface TripSummaryOut {
  id: string
  name: string
  start_date: string
  total_days: number
  status: 'draft' | 'confirmed'
  city_names: string[]
  updated_at: string
}

export interface TripDetailOut {
  id: string
  name: string
  status: 'draft' | 'confirmed'
  start_date: string
  end_date: string
  total_days: number
  city_names: string[]
  query: string
  created_at: string
  updated_at: string
  stays: StayOut[]
  transfers: TransferOut[]
  budget: BudgetPanelOut
}

export interface CityOut {
  name: string
  adcode: string
  level: string
  province: string | null
  lat_gcj02: number | null
  lng_gcj02: number | null
}

export interface CreateTripIn {
  start_date: string
  cities: { name: string; days: number }[]
  name?: string
}

export interface UpdateStaysIn {
  cities: { name: string; days: number }[]
  start_date?: string
}

/** 后端在错误响应里给的结构化信息。 */
export interface ApiErrorBody {
  error?: string
  message?: string
  detail?: string
  missing_fields?: string[]
  missing_keys?: string[]
  city_names?: string[]
}

export class ApiError extends Error {
  readonly status: number
  readonly body: ApiErrorBody

  constructor(status: number, body: ApiErrorBody) {
    super(body.message ?? body.detail ?? `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }

  /** 用户能据以行动的建议。空屏与失败都该给方向，不该只给情绪。 */
  get hint(): string | null {
    switch (this.body.error) {
      case 'config_missing':
        return `在项目根目录的 .env.local 里填上这些，然后重启服务。`
      case 'city_not_found':
        return `试试换一种写法，或先在「运行状态」里确认高德 Key 已配置。`
      case 'missing_input':
        return `把缺的信息补进需求描述里再试一次。`
      case 'amap_unavailable':
        return `高德接口不可用。检查 .env.local 里的 AMAP_API_KEY 与网络。`
      case 'plan_conversion_failed':
        return `引擎给出了无法解析的结果，这通常是引擎侧的问题。`
      default:
        return null
    }
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  })

  if (response.status === 204) {
    return undefined as T
  }

  const text = await response.text()
  let parsed: unknown = null
  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      parsed = { message: text }
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, (parsed ?? {}) as ApiErrorBody)
  }
  return parsed as T
}

const jsonInit = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

// ─── 系统接口 ────────────────────────────────────────────────

export const fetchHealth = (): Promise<HealthResponse> => request('/api/health')
export const fetchClientConfig = (): Promise<ClientConfig> => request('/api/config')

// ─── 行程接口 ────────────────────────────────────────────────

export const listTrips = (): Promise<TripSummaryOut[]> => request('/api/trips')

export const getTrip = (tripId: string): Promise<TripDetailOut> =>
  request(`/api/trips/${encodeURIComponent(tripId)}`)

export const createTrip = (payload: CreateTripIn): Promise<TripDetailOut> =>
  request('/api/trips', jsonInit(payload))

export const deleteTrip = (tripId: string): Promise<void> =>
  request(`/api/trips/${encodeURIComponent(tripId)}`, { method: 'DELETE' })

export const confirmTrip = (tripId: string): Promise<TripDetailOut> =>
  request(`/api/trips/${encodeURIComponent(tripId)}/confirm`, { method: 'POST' })

/** 改城市与天数。总天数与日期由服务端重算。 */
export const updateStays = (tripId: string, payload: UpdateStaysIn): Promise<TripDetailOut> =>
  request(`/api/trips/${encodeURIComponent(tripId)}/stays`, {
    ...jsonInit(payload),
    method: 'PUT',
  })

/** 重新查一遍城际车次。会真的问 12306，慢几秒。 */
export const refreshTransfers = (tripId: string): Promise<TransferOut[]> =>
  request(`/api/trips/${encodeURIComponent(tripId)}/transfers/refresh`, { method: 'POST' })

/** 按城市分别取天气预报。 */
export const fetchTripWeather = (tripId: string): Promise<TripWeatherOut> =>
  request(`/api/trips/${encodeURIComponent(tripId)}/weather`)

export const resolveCity = (name: string): Promise<CityOut[]> =>
  request(`/api/cities/resolve?name=${encodeURIComponent(name)}`)

// ─── 生成（流式）──────────────────────────────────────────────

export interface PlanIn {
  query: string
  start_date?: string
  days?: number
  name?: string
}

export type PlanEvent =
  | { type: 'stage'; node: string; label: string }
  | { type: 'done'; tripId: string; name: string; totalDays: number; cityNames: string[] }
  | { type: 'error'; body: ApiErrorBody }

/** 解析一帧 SSE：`event: 名\ndata: JSON`。 */
function parseFrame(frame: string): PlanEvent | null {
  let name = ''
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event: ')) name = line.slice('event: '.length)
    else if (line.startsWith('data: ')) data = line.slice('data: '.length)
  }
  if (!name) return null

  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(data) as Record<string, unknown>
  } catch {
    return null
  }

  switch (name) {
    case 'stage':
      return {
        type: 'stage',
        node: String(payload.node ?? ''),
        label: String(payload.label ?? ''),
      }
    case 'done':
      return {
        type: 'done',
        tripId: String(payload.trip_id ?? ''),
        name: String(payload.name ?? ''),
        totalDays: Number(payload.total_days ?? 0),
        cityNames: Array.isArray(payload.city_names) ? (payload.city_names as string[]) : [],
      }
    case 'error':
      return { type: 'error', body: payload as ApiErrorBody }
    default:
      return null
  }
}

/**
 * 生成行程，边跑边把阶段事件交给调用方。
 *
 * 用 fetch + 手工解析，不用 EventSource：EventSource 只支持 GET，而这个请求
 * 会创建一份行程。另外 EventSource 失败会自动重连——那会把一次生成变成两次。
 */
export async function streamPlan(
  payload: PlanIn,
  onEvent: (event: PlanEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response
  try {
    response = await fetch('/api/plan/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(payload),
      signal,
    })
  } catch (cause) {
    if (signal?.aborted) return
    onEvent({
      type: 'error',
      body: { message: cause instanceof Error ? cause.message : String(cause) },
    })
    return
  }

  if (!response.ok) {
    const text = await response.text()
    let body: ApiErrorBody = { message: text }
    try {
      body = JSON.parse(text) as ApiErrorBody
    } catch {
      /* 不是 JSON 就用原文 */
    }
    onEvent({ type: 'error', body })
    return
  }

  if (!response.body) {
    onEvent({ type: 'error', body: { message: '浏览器没有给出可读的响应流' } })
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const event = parseFrame(frame)
        if (event) onEvent(event)
        boundary = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}
