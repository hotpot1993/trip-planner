/**
 * 按指定宽度给页面截图，并顺手量一次横向溢出。
 *
 * 为什么不用 `chrome --screenshot`：那个方式下 `--window-size` 与真实的布局
 * 视口不一定相等（实测要 390 却渲染成 489），截出来的图会**看起来**被切掉，
 * 让人去修一个并不存在的移动端 bug。这个脚本用 CDP 的
 * `Emulation.setDeviceMetricsOverride` 把视口钉死，再截图，所见即所得。
 *
 * 用法（需要先跑起 `ls serve`）：
 *     node scripts/shot.mjs <url> <width> <out.png> [fullPageHeight] [clickText]
 *
 * `clickText` 可选：截图前点一下第一个文字包含它的元素。用来截「点开之后」
 * 的界面（选中另一篇素材、打开某个开关），否则只能截到默认状态。
 *
 * Node 24 自带 WebSocket，不需要装 puppeteer。
 */

const [, , target, widthArg, out, heightArg, clickText] = process.argv
if (!target || !widthArg || !out) {
  console.error('用法：node scripts/shot.mjs <url> <width> <out.png> [height]')
  process.exit(2)
}

const width = Number(widthArg)
const height = Number(heightArg ?? 1200)
const port = 9333

const { spawn } = await import('node:child_process')
const { tmpdir } = await import('node:os')
const { writeFile } = await import('node:fs/promises')

const chrome = spawn(
  process.env.CHROME_PATH ?? 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  [
    '--headless=new',
    '--disable-gpu',
    '--hide-scrollbars',
    `--remote-debugging-port=${port}`,
    '--no-first-run',
    `--user-data-dir=${tmpdir()}/lushu-cdp-shot`,
    'about:blank',
  ],
  { stdio: 'ignore' },
)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function findPage() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
      const page = list.find((item) => item.type === 'page')
      if (page) return page
    } catch {
      /* 还没起来 */
    }
    await sleep(250)
  }
  throw new Error('连不上 Chrome 的调试端口')
}

const page = await findPage()
const socket = new WebSocket(page.webSocketDebuggerUrl)
let nextId = 1
const pending = new Map()
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data)
  const resolve = pending.get(message.id)
  if (resolve) {
    pending.delete(message.id)
    resolve(message.result)
  }
})
await new Promise((resolve) => socket.addEventListener('open', resolve))

function send(method, params = {}) {
  const id = nextId++
  socket.send(JSON.stringify({ id, method, params }))
  return new Promise((resolve) => pending.set(id, resolve))
}

await send('Page.enable')
await send('Emulation.setDeviceMetricsOverride', {
  width,
  height,
  deviceScaleFactor: 1,
  mobile: width < 640,
})
await send('Page.navigate', { url: target })
await sleep(3000)

if (clickText) {
  const clicked = await send('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const wanted = ${JSON.stringify(clickText)}
      const target = [...document.querySelectorAll('button, a, label')]
        .find((el) => (el.textContent || '').includes(wanted))
      if (!target) return 'not-found'
      target.click()
      return 'clicked:' + (target.textContent || '').trim().slice(0, 24)
    })()`,
  })
  console.log(`点击 ${clickText} → ${clicked.result.value}`)
  await sleep(1500)
}

// 只报**不在横向滚动容器里**的越界元素：滚动容器内部的越界是设计的一部分
const probe = `(() => {
  const vw = document.documentElement.clientWidth
  const scrollable = (el) => {
    for (let node = el.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node)
      if (style.overflowX === 'auto' || style.overflowX === 'scroll') return true
    }
    return false
  }
  const rows = []
  for (const el of document.querySelectorAll('*')) {
    const rect = el.getBoundingClientRect()
    if (rect.width === 0 || scrollable(el)) continue
    const over = Math.round(rect.right - vw)
    if (over > 2) {
      rows.push({
        over,
        tag: el.tagName.toLowerCase(),
        cls: (el.className || '').toString().slice(0, 80),
        text: (el.textContent || '').trim().slice(0, 24),
      })
    }
  }
  rows.sort((a, b) => b.over - a.over)
  return JSON.stringify({
    viewport: vw,
    scrollWidth: document.documentElement.scrollWidth,
    offenders: rows.slice(0, 8),
  }, null, 2)
})()`

const measured = await send('Runtime.evaluate', { expression: probe, returnByValue: true })
console.log(measured.result.value)

const shot = await send('Page.captureScreenshot', {
  format: 'png',
  captureBeyondViewport: true,
})
await writeFile(out, Buffer.from(shot.data, 'base64'))
console.log(`已写入 ${out}`)

socket.close()
chrome.kill()
