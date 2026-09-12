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

export const resolveCity = (name: string): Promise<CityOut[]> =>
  request(`/api/cities/resolve?name=${encodeURIComponent(name)}`)
