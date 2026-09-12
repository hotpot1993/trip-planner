/**
 * 极简哈希路由。
 *
 * 只有三个视图，不值得为它引一个路由库。用哈希是为了让浏览器的后退键能用、
 * 刷新不丢位置——这两件事对「边看边改行程」是真需求。
 */

import { useEffect, useState } from 'react'

export type Route =
  | { name: 'trips' }
  | { name: 'trip'; tripId: string }
  | { name: 'status' }

export function parseHash(hash: string): Route {
  const path = hash.replace(/^#\/?/, '')
  const segments = path.split('/').filter(Boolean)

  const [head, second] = segments
  if (head === 'trips' && second) {
    return { name: 'trip', tripId: decodeURIComponent(second) }
  }
  if (head === 'status') {
    return { name: 'status' }
  }
  return { name: 'trips' }
}

export function hrefFor(route: Route): string {
  switch (route.name) {
    case 'trip':
      return `#/trips/${encodeURIComponent(route.tripId)}`
    case 'status':
      return '#/status'
    default:
      return '#/'
  }
}

export function navigate(route: Route): void {
  window.location.hash = hrefFor(route)
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash))

  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash))
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  return route
}
