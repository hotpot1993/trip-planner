/**
 * 后端接口的类型与调用。
 *
 * 字段名与 FastAPI 的响应逐字对应，改后端时这里必须同步改。
 * 一律用相对路径 `/api`：开发期由 Vite 转发到本地后端，生产期由后端自己托管。
 */

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

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: 'application/json' } })
  if (!response.ok) {
    throw new Error(`请求 ${path} 失败：HTTP ${response.status}`)
  }
  return (await response.json()) as T
}

export const fetchHealth = (): Promise<HealthResponse> => getJson<HealthResponse>('/api/health')

export const fetchClientConfig = (): Promise<ClientConfig> => getJson<ClientConfig>('/api/config')
