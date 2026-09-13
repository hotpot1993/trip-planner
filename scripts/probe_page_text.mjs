/**
 * 把页面上某段文字的真实内容打出来。
 *
 * 截图会骗人：缩到一半的等宽 POI id 与「没填对齐目标」在图上长得差不多，
 * 靠看图判断界面状态是不可靠的。这个脚本直接问 DOM。
 *
 * 用法（需要先跑起 `ls serve`）：
 *     node scripts/probe_page_text.mjs <url> <选择器> [clickText]
 */

const [, , target, selector, clickText] = process.argv
if (!target || !selector) {
  console.error('用法：node scripts/probe_page_text.mjs <url> <选择器> [clickText]')
  process.exit(2)
}

const port = 9334
const { spawn } = await import('node:child_process')
const { tmpdir } = await import('node:os')

const chrome = spawn(
  process.env.CHROME_PATH ?? 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  [
    '--headless=new',
    '--disable-gpu',
    `--remote-debugging-port=${port}`,
    '--no-first-run',
    `--user-data-dir=${tmpdir()}/lushu-cdp-text`,
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
  width: 1280,
  height: 1200,
  deviceScaleFactor: 1,
  mobile: false,
})
await send('Page.navigate', { url: target })
await sleep(3000)

if (clickText) {
  await send('Runtime.evaluate', {
    returnByValue: true,
    expression: `(() => {
      const wanted = ${JSON.stringify(clickText)}
      const el = [...document.querySelectorAll('button, a, label')]
        .find((node) => (node.textContent || '').includes(wanted))
      if (el) el.click()
      return Boolean(el)
    })()`,
  })
  await sleep(1500)
}

const probe = `(() => {
  const nodes = [...document.querySelectorAll(${JSON.stringify(selector)})]
  return JSON.stringify(nodes.map((node) => (node.textContent || '').trim()), null, 2)
})()`

const result = await send('Runtime.evaluate', { expression: probe, returnByValue: true })
console.log(result.result.value)

socket.close()
chrome.kill()
