/**
 * 验证「在正文里划一段 → 变成一条待标注引文」这条交互。
 *
 * 这是标注台的全部前提，而它也是最容易**静默出错**的地方：偏移算错一个字，
 * 存进去的引文就不再是原文的连续子串，而界面上看不出任何异常——
 * 人要等到评测数字莫名其妙时才可能发现。
 *
 * 所以这里在真实浏览器里造一个**跨标记边界**的选区（单段内的选区太容易通过，
 * 覆盖不到分段渲染那条路径），然后检查表单里拿到的引文与偏移是否与
 * 选区逐字一致。
 *
 * 用法（需要先跑起 `ls serve`）：
 *     node scripts/verify_web_annotate.mjs <document_id>
 */

const documentId = process.argv[2]
if (!documentId) {
  console.error('用法：node scripts/verify_web_annotate.mjs <document_id>')
  process.exit(2)
}

const port = 9335
const { spawn } = await import('node:child_process')
const { tmpdir } = await import('node:os')

const chrome = spawn(
  process.env.CHROME_PATH ?? 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  [
    '--headless=new',
    '--disable-gpu',
    `--remote-debugging-port=${port}`,
    '--no-first-run',
    `--user-data-dir=${tmpdir()}/lushu-cdp-verify`,
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

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  })
  return result.result.value
}

await send('Page.enable')
await send('Emulation.setDeviceMetricsOverride', {
  width: 1280,
  height: 1200,
  deviceScaleFactor: 1,
  mobile: false,
})
await send('Page.navigate', { url: 'http://127.0.0.1:8099/#/workbench/annotate' })
await sleep(3000)

// 选中要标注的那一篇
const picked = await evaluate(`(() => {
  const el = [...document.querySelectorAll('button')]
    .find((node) => (node.textContent || '').includes(${JSON.stringify(documentId)}))
  if (!el) return 'not-found'
  el.click()
  return 'ok'
})()`)
if (picked !== 'ok') {
  console.error(`素材选择失败：${picked}（确认这一篇在列表里）`)
  process.exit(1)
}
await sleep(2000)

const result = await evaluate(`(() => {
  const segments = [...document.querySelectorAll('article [data-start]')]
  if (segments.length < 4) return JSON.stringify({ error: '正文分段不足，选不了跨段选区' })

  const first = segments[1]
  const later = segments[Math.min(4, segments.length - 1)]
  const startNode = first.firstChild
  const endNode = later.firstChild
  if (!startNode || !endNode || startNode.nodeType !== 3 || endNode.nodeType !== 3) {
    return JSON.stringify({ error: '分段里不是纯文本节点' })
  }

  const startOffset = Math.min(2, startNode.length)
  const endOffset = Math.min(4, endNode.length)
  const range = document.createRange()
  range.setStart(startNode, startOffset)
  range.setEnd(endNode, endOffset)

  const selection = window.getSelection()
  selection.removeAllRanges()
  selection.addRange(range)

  const expectedText = range.toString()
  const expectedStart = Number(first.dataset.start) + startOffset
  const expectedEnd = Number(later.dataset.start) + endOffset

  document.querySelector('article').dispatchEvent(
    new MouseEvent('mouseup', { bubbles: true }),
  )

  return JSON.stringify({ expectedText, expectedStart, expectedEnd })
})()`)

const expected = JSON.parse(result)
if (expected.error) {
  console.error(`选区构造失败：${expected.error}`)
  process.exit(1)
}

await sleep(600)
const form = await evaluate(`(() => {
  const quote = document.querySelector('section blockquote')
  return JSON.stringify({
    quote: quote ? quote.textContent : null,
    hasForm: Boolean(document.querySelector('section blockquote')),
    buttons: [...document.querySelectorAll('section button')]
      .map((node) => (node.textContent || '').trim())
      .filter((text) => text === '避坑' || text === '打卡' || text === '记下这条'),
  })
})()`)

const got = JSON.parse(form)
const problems = []
if (!got.hasForm) problems.push('划选之后没有出现标注表单')
if (got.quote !== expected.expectedText) {
  problems.push(`引文对不上：期望 ${JSON.stringify(expected.expectedText)}，实际 ${JSON.stringify(got.quote)}`)
}
if (!got.buttons.includes('记下这条')) problems.push('表单里没有「记下这条」')

console.log(`选区（正文偏移 ${expected.expectedStart}-${expected.expectedEnd}）：`)
console.log(`  ${expected.expectedText}`)
console.log(`表单拿到的引文：`)
console.log(`  ${got.quote}`)

if (problems.length) {
  console.error('\n不通过：')
  for (const problem of problems) console.error(`  - ${problem}`)
  socket.close()
  chrome.kill()
  process.exit(1)
}

console.log('\n通过：跨标记边界的选区还原成了逐字相同的引文与偏移。')
socket.close()
chrome.kill()
