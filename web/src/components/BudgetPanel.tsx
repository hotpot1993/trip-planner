import type { BudgetPanelOut } from '@/lib/api'

const CATEGORY_LABELS: Record<string, string> = {
  intercity: '城际交通',
  local: '市内交通',
  lodging: '住宿',
  meal: '餐饮',
  ticket: '门票',
  other: '其它',
}

/**
 * 预算面板。**城际交通单独成栏**——它是多城市行程独有的开销，
 * 混进总账里就看不出「这趟多花的钱其实都在路上」。
 *
 * 参考价必须标出来。火车票是实价还是估价，用户得能分辨——把估价说成实价
 * 是他做预算时最容易被坑的地方。
 */
export function BudgetPanel({ budget }: { budget: BudgetPanelOut }) {
  const empty = budget.intercity.length === 0 && budget.others.length === 0

  return (
    <section className="rounded border border-rule bg-paper-card p-5">
      <h2 className="font-display text-xl">预算</h2>

      {empty ? (
        <p className="mt-3 text-sm text-ink-3">
          还没有预算项。生成行程之后，城际交通的费用会自动记进这里。
        </p>
      ) : (
        <>
          <div className="mt-4">
            <div className="flex items-baseline justify-between">
              <h3 className="text-sm text-ink">城际交通</h3>
              <span className="data text-sm">¥{formatMoney(budget.intercity_total)}</span>
            </div>
            {budget.intercity.length === 0 ? (
              <p className="mt-1 text-xs text-ink-3">
                没有城际交通项。单城市行程不会有；多城市行程刷新车次后会出现。
              </p>
            ) : (
              <ul className="mt-1.5 m-0 list-none space-y-0.5 p-0">
                {budget.intercity.map((item) => (
                  <li key={item.label} className="flex items-baseline gap-x-3 text-sm">
                    <span className="min-w-0 flex-1 truncate text-ink-2" title={item.label}>
                      {item.label}
                    </span>
                    <span className="data shrink-0">¥{formatMoney(item.amount)}</span>
                    {item.is_reference_price ? (
                      <span className="shrink-0 text-xs text-ink-3">参考价</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {budget.others.length > 0 ? (
            <div className="mt-4 border-t border-rule pt-3">
              <div className="flex items-baseline justify-between">
                <h3 className="text-sm text-ink">其它</h3>
                <span className="data text-sm">¥{formatMoney(budget.other_total)}</span>
              </div>
              <ul className="mt-1.5 m-0 list-none space-y-0.5 p-0">
                {budget.others.map((item) => (
                  <li key={item.label} className="flex items-baseline gap-x-3 text-sm">
                    <span className="w-16 shrink-0 text-xs text-ink-3">
                      {CATEGORY_LABELS[item.category] ?? item.category}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-ink-2" title={item.label}>
                      {item.label}
                    </span>
                    <span className="data shrink-0">¥{formatMoney(item.amount)}</span>
                    {item.is_reference_price ? (
                      <span className="shrink-0 text-xs text-ink-3">参考价</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="mt-4 flex items-baseline justify-between border-t border-rule pt-3">
            <span className="text-sm">合计</span>
            <span className="data">¥{formatMoney(budget.total)}</span>
          </div>

          {budget.has_reference_prices ? (
            <p className="mt-2 text-xs text-ink-3">
              标注「参考价」的金额是按里程或公开信息估算的，不是实付价。以购票时的实际价格为准。
            </p>
          ) : null}
        </>
      )}
    </section>
  )
}

/** 金额保留两位小数但去掉无意义的 .00。 */
function formatMoney(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}
