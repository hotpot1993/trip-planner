import type { ReactNode } from 'react'

import { hrefFor, useRoute } from '@/lib/router'
import { Status } from '@/views/Status'
import { TripDetail } from '@/views/TripDetail'
import { TripList } from '@/views/TripList'

export default function App() {
  const route = useRoute()

  return (
    <div className="min-h-dvh">
      <header className="border-b border-rule">
        <div className="mx-auto flex max-w-3xl flex-wrap items-baseline gap-x-6 gap-y-2 px-6 py-5">
          <a href={hrefFor({ name: 'trips' })} className="no-underline">
            <span className="font-display text-2xl tracking-wide text-ink">路书</span>
          </a>
          <nav className="flex gap-x-4 text-sm">
            <NavLink href={hrefFor({ name: 'trips' })} active={route.name !== 'status'}>
              行程
            </NavLink>
            <NavLink href={hrefFor({ name: 'status' })} active={route.name === 'status'}>
              运行状态
            </NavLink>
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 py-8">
        {route.name === 'trips' ? <TripList /> : null}
        {route.name === 'trip' ? <TripDetail tripId={route.tripId} /> : null}
        {route.name === 'status' ? <Status /> : null}
      </main>

      <footer className="mx-auto max-w-3xl px-6 pb-10 text-xs leading-relaxed text-ink-3">
        本地优先的旅行攻略规划工具。多城市规划是骨架，攻略提纯与景点预约是挂在它上面的信息层。
      </footer>
    </div>
  )
}

function NavLink({
  href,
  active,
  children,
}: {
  href: string
  active: boolean
  children: ReactNode
}) {
  return (
    <a
      href={href}
      aria-current={active ? 'page' : undefined}
      className={
        active
          ? 'text-ink underline decoration-azurite decoration-2 underline-offset-4'
          : 'text-ink-3 no-underline hover:text-ink'
      }
    >
      {children}
    </a>
  )
}
