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

// ─── 预约 ────────────────────────────────────────────────────

export interface BookingChannelOut {
  name: string
  kind: string
  url: string | null
}

export type Urgency = 'overdue' | 'today' | 'soon' | 'later'

export interface BookingAlertOut {
  poi_id: string
  poi_name: string
  city_name: string | null
  visit_date: string
  release_date: string | null
  days_until_release: number | null
  urgency: Urgency
  /** 给用户看的一行结论，由领域层算好——界面不自己拼文案。 */
  headline: string
  release_time: string | null
  channels: BookingChannelOut[]
  requires_real_name: boolean | null
  id_required_note: string | null
  /** 复核时写下的「坑」。它原本只出现在复核界面，等于查到了却没告诉用户。 */
  note: string | null
}

export interface TripBookingOut {
  trip_id: string
  today: string
  alerts: BookingAlertOut[]
  /** 有规则但规则还是草案的景点。清单里没有 ≠ 不用预约。 */
  pending_review: string[]
}

export const fetchTripBooking = (tripId: string): Promise<TripBookingOut> =>
  request(`/api/trips/${encodeURIComponent(tripId)}/booking`)

/** 预约日历的下载地址。日历是唯一的推送通道，所以直接给链接而不走 fetch。 */
export const bookingCalendarUrl = (tripId: string): string =>
  `/api/trips/${encodeURIComponent(tripId)}/booking.ics`

// ─── 预约规则复核（工作台的第三类队列）─────────────────────────

export interface BookingRuleOut {
  poi_id: string
  poi_name: string
  status: 'draft' | 'reviewed'
  booking_required: boolean
  advance_days: number | null
  release_time: string | null
  channels: BookingChannelOut[]
  requires_real_name: boolean | null
  id_required_note: string | null
  closed_days_weekdays: number[]
  evidence_url: string | null
  reviewed_at: string | null
  verify_due_at: string | null
  reviewer_note: string | null
  /** 体检结果与规则一起返回：复核是「看着体检结果签字」。 */
  errors: string[]
  warnings: string[]
}

export const listBookingRules = (pendingOnly = false): Promise<BookingRuleOut[]> =>
  request(`/api/workbench/booking-rules?pending_only=${pendingOnly ? 'true' : 'false'}`)

/** 复核或撤回复核。有 error 时服务端会 422，带上体检文案。 */
export const reviewBookingRule = (
  poiId: string,
  payload: { evidence_url?: string; note?: string; revoke?: boolean } = {},
): Promise<BookingRuleOut> =>
  request(`/api/workbench/booking-rules/${encodeURIComponent(poiId)}/review`, jsonInit(payload))

// ─── 数据工作台 ──────────────────────────────────────────────

export type Polarity = 'avoid' | 'highlight'

export interface GoldDocumentOut {
  document_id: string
  title: string | null
  site: string
  chars: number
  prediction_count: number
  labeled: number
  in_gold_set: boolean
  model_output_seen: boolean
  annotated_at: string | null
}

export interface GoldLabelOut {
  label_id: string
  quote: string
  polarity: Polarity
  subject_type: string
  subject_name: string | null
  expected_poi_id: string | null
  facet: string | null
  char_start: number | null
  char_end: number | null
  verdict: string
  note: string | null
  created_at: string
}

/** 模型抽出的一条候选。界面上必须标明「这是模型说的，不是原文」。 */
export interface GoldCandidateOut {
  subject_name: string
  polarity: Polarity
  facet: string
  text: string
  quote: string
  char_start: number | null
  char_end: number | null
}

export interface GoldContextOut {
  document_id: string
  title: string | null
  body: string
  in_gold_set: boolean
  model_output_seen: boolean
  labels: GoldLabelOut[]
  candidates: GoldCandidateOut[]
}

export interface GoldLabelIn {
  quote: string
  polarity: Polarity
  subject_type?: string
  subject_name?: string | null
  expected_poi_id?: string | null
  facet?: string | null
  note?: string | null
}

export interface LocalPoiOut {
  poi_id: string
  name: string
  label: string
  city_name: string | null
  parent_id: string | null
  parent_name: string | null
  typecode: string | null
  address: string | null
  is_root: boolean
}

export interface GoldScoreOut {
  document_id: string
  hits: number
  gold_total: number
  predicted_total: number
  recall: number | null
  precision: number | null
  alignment_accuracy: number | null
  unaligned: number
  missed: string[]
  spurious: string[]
}

export interface EvalOut {
  sample_size: number
  ready: boolean
  gold_labels: number
  gold_documents: number
  blind_documents: number
  hits: number
  gold_total: number
  predicted_total: number
  recall: number | null
  precision: number | null
  alignment_accuracy: number | null
  unaligned: number
  misaligned: number
  discard_ratio: number | null
  documents: GoldScoreOut[]
}

export interface CandidatePoiOut {
  poi_id: string
  name: string
  typecode: string | null
  address: string | null
  adcode: string | null
  name_score: number | null
  usable: boolean
  reject_reason: string | null
}

export interface PendingClaimOut {
  subject_name: string
  subject_type: string
  polarity: Polarity
  facet: string
  text: string
  quote: string
  char_start: number | null
  char_end: number | null
  quote_verdict: string | null
}

export interface AlignmentTaskOut {
  task_id: string
  mention_name: string
  city_adcode: string | null
  context_snippet: string | null
  source_document_id: string | null
  source_title: string | null
  candidates: CandidatePoiOut[]
  claims: PendingClaimOut[]
  created_at: string
}

export interface ClaimOut {
  claim_id: string
  subject_type: string
  subject_name: string | null
  poi_id: string | null
  city_adcode: string | null
  polarity: Polarity
  facet: string
  text: string
  confidence: 'high' | 'single_source'
  independent_source_count: number
  status: string
  first_seen_at: string
  verify_due_at: string | null
  evidence_count: number
}

export interface EvidenceOut {
  quote: string
  site: string
  url: string | null
  title: string | null
  char_start: number | null
  char_end: number | null
}

export interface ExtractionRunOut {
  run_id: string
  source_document_id: string
  source_title: string | null
  model: string
  prompt_version: string
  candidate_count: number
  accepted_count: number
  dropped_count: number
  duration_ms: number | null
  status: string
  error: string | null
  created_at: string
}

export interface PipelineStatsOut {
  documents: number
  groups: number
  claims: number
  high_confidence: number
  evidence: number
  extract_documents: number
  extract_candidates: number
  extract_accepted: number
  extract_dropped: number
  align_pending: number
  align_resolved: number
  align_discarded: number
}

export interface IngestOut {
  document_id: string
  duplicate: 'new' | 'repost' | 'exact'
  site: string
  chars: number
  duplicate_of: string | null
  coverage: number | null
  group_id: string | null
}

export const listGoldDocuments = (): Promise<GoldDocumentOut[]> =>
  request('/api/workbench/gold')

export const getGoldContext = (documentId: string): Promise<GoldContextOut> =>
  request(`/api/workbench/gold/${encodeURIComponent(documentId)}`)

export const addGoldLabel = (
  documentId: string,
  payload: GoldLabelIn,
): Promise<GoldLabelOut> =>
  request(`/api/workbench/gold/${encodeURIComponent(documentId)}/labels`, jsonInit(payload))

export const deleteGoldLabel = (labelId: string): Promise<void> =>
  request(`/api/workbench/gold/labels/${encodeURIComponent(labelId)}`, { method: 'DELETE' })

/**
 * 补全一条已有标注。
 *
 * 标注是来回的：先照着原文把事实写下来，再去查这个提及指的是哪个景点。
 * `setPoi` 才写 `expected_poi_id`——它要能表达「把 POI 清掉」，
 * 也要能表达「这次不改 POI」，两者不能都用省略表示。
 */
export const patchGoldLabel = (
  labelId: string,
  payload: {
    expected_poi_id?: string | null
    set_poi?: boolean
    subject_name?: string
    facet?: string
    note?: string
  },
): Promise<GoldLabelOut> =>
  request(`/api/workbench/gold/labels/${encodeURIComponent(labelId)}`, {
    ...jsonInit(payload),
    method: 'PATCH',
  })

export const markGoldDone = (
  documentId: string,
  payload: { done: boolean; model_output_seen?: boolean; annotator?: string },
): Promise<GoldDocumentOut> =>
  request(`/api/workbench/gold/${encodeURIComponent(documentId)}/done`, jsonInit(payload))

export const runEvaluation = (): Promise<EvalOut> => request('/api/workbench/eval')

export const listAlignments = (): Promise<AlignmentTaskOut[]> =>
  request('/api/workbench/alignments')

export const resolveAlignment = (
  taskId: string,
  payload: { poi_id?: string | null; discard?: boolean },
): Promise<ClaimOut | null> =>
  request(`/api/workbench/alignments/${encodeURIComponent(taskId)}/resolve`, jsonInit(payload))

export const listExtractions = (): Promise<ExtractionRunOut[]> =>
  request('/api/workbench/extractions')

export const workbenchStats = (): Promise<PipelineStatsOut> => request('/api/workbench/stats')

export const searchLocalPois = (q: string, limit = 20): Promise<LocalPoiOut[]> =>
  request(`/api/workbench/pois?q=${encodeURIComponent(q)}&limit=${limit}`)

export const ingestDocument = (payload: {
  body: string
  title?: string
  url?: string
  author?: string
  site?: string
}): Promise<IngestOut> => request('/api/ingest', jsonInit(payload))

// ─── 管线（流式）──────────────────────────────────────────────

export type PipelineStep = 'extract' | 'align' | 'group' | 'merge'

export interface PipelineDonePayload {
  extract?: {
    documents: number
    cached: number
    accepted: number
    dropped: number
    failed: { document_id: string; error: string }[]
  }
  align?: {
    mentions: number
    aligned: number
    collapsed: number
    pending: number
    unresolved_subjects: number
    failed: { mention: string; error: string }[]
  }
  group?: { compared: number; merged: number; groups: number }
  merge?: { created: number; extended: number; high_confidence: number; single_source: number }
  stats?: PipelineStatsOut
}

export type PipelineEvent =
  | { type: 'stage'; step: PipelineStep; label: string }
  | { type: 'progress'; step: PipelineStep; label: string; index: number; total: number }
  | { type: 'done'; payload: PipelineDonePayload }
  | { type: 'error'; body: ApiErrorBody }

/** 管线与规划共用一套 SSE 形状，所以复用同一个分帧解析。 */
function parsePipelineFrame(frame: string): PipelineEvent | null {
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
        step: String(payload.step ?? '') as PipelineStep,
        label: String(payload.label ?? ''),
      }
    case 'progress':
      return {
        type: 'progress',
        step: String(payload.step ?? '') as PipelineStep,
        label: String(payload.label ?? ''),
        index: Number(payload.index ?? 0),
        total: Number(payload.total ?? 0),
      }
    case 'done':
      return { type: 'done', payload: payload as PipelineDonePayload }
    case 'error':
      return { type: 'error', body: payload as ApiErrorBody }
    default:
      return null
  }
}

/** 跑一轮管线，边跑边把进度交给调用方。跑一轮可能几分钟。 */
export async function streamPipeline(
  payload: { steps: PipelineStep[]; limit?: number; force?: boolean },
  onEvent: (event: PipelineEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response
  try {
    response = await fetch('/api/pipeline/run', {
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

  if (!response.ok || !response.body) {
    const text = await response.text()
    let body: ApiErrorBody = { message: text || '管线没有返回可读的响应' }
    try {
      body = JSON.parse(text) as ApiErrorBody
    } catch {
      /* 不是 JSON 就用原文 */
    }
    onEvent({ type: 'error', body })
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
        const event = parsePipelineFrame(frame)
        if (event) onEvent(event)
        boundary = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}

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
