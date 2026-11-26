import assert from 'node:assert/strict'
import test from 'node:test'
import { apply } from '../integrations/dsh/site-entry/guard.mjs'

const site = 'https://public.example/demo'

// 仅通过 rc.5 的公开事件与工具守卫契约驱动插件，不读取用户宿主数据。
function host() {
  let observe, guard, ready
  apply({
    on(name, listener) { assert.equal(name, 'session/event'); observe = listener },
    tools: { guard(callback) { guard = callback } },
    provide(name) { ready = name },
  })
  assert.equal(ready, 'atlasPublicInputGuard')
  const session = {}

  // 模拟一条由原生会话控制器写入的消息，支持替换会话、来源及事件类型。
  function event(text, source = { kind: 'user', rpcId: 'fixture' }, type = 'user/message', target = session) {
    observe(target, { type, data: { source, content: [{ type: 'text', text }] } })
  }

  // 模拟宿主在一份已确定的会话作用域中分派模型参数。
  function open(url, environment, current = session) {
    return guard({ name: 'mcp__atlas__open_site', arguments: { url, environment }, agent: { session: current } })
  }
  // 分派带真实 callId 的宿主工具，随后能按同一 ID 投递该工具的原生结果。
  function dispatch(name, args, callId, target = session) {
    return guard({ name, arguments: args, callId, agent: { session: target } })
  }
  // 将 MCP 文本结果或原生追问答案以 DSH 的 tool/result 结构交给守卫。
  function result(toolCallId, value, target = session) {
    observe(target, { type: 'tool/result', data: { message: { toolCallId, content: [{ type: 'text', text: JSON.stringify(value) }] } } })
  }
  return { event, open, guard, dispatch, result, session }
}

test('用户在当前会话给出的公开地址与对应环境可用，标签不能被模型改写或省略', () => {
  const { event, open } = host()
  event('这是 Aweb 的测试环境，' + site + '。')
  assert.equal(open(site, '测试'), undefined)
  assert.equal(open(site, '测试环境'), undefined)
  assert.ok(open(site, '生产'))
  assert.ok(open(site))
  assert.ok(open(site + '/other', '测试'))
  event(site + ' 是生产环境，先打开。')
  assert.equal(open(site, '生产环境'), undefined)
  assert.ok(open(site, '测试'))
  event('访问 ' + site + '，环境：UAT')
  assert.equal(open(site, 'UAT'), undefined)
  assert.ok(open(site, '生产'))
  event('访问 https://test.example/')
  assert.equal(open('https://test.example'), undefined)
  assert.ok(open('https://test.example', '测试'))
  event('请打开 ' + site + '，环境名是「测试环境」。请读取第一页，告诉我页面标题、所在环境和本次完整运行 ID')
  assert.equal(open(site, '测试环境'), undefined)
  event('这是测试环境 ' + site + '，先看看')
  assert.equal(open(site, '测试环境'), undefined)
  event('这是测试用的 ' + site)
  assert.equal(open(site, '测试环境'), undefined)
  for (const label of ['QA', 'UAT', 'staging']) {
    event('在 ' + label + ' 打开 ' + site)
    assert.equal(open(site, label), undefined)
    assert.ok(open(site, '生产环境'))
    assert.ok(open(site))
  }
  event('这是沙箱环境，' + site)
  assert.equal(open(site, '沙箱环境'), undefined)
  assert.ok(open(site, '测试环境'))
  assert.ok(open(site))
  event('访问 Aweb 的压测环境，' + site)
  assert.equal(open(site, '压测环境'), undefined)
  assert.ok(open(site, '生产环境'))
  event('I want to test ' + site + ' in production')
  assert.equal(open(site, 'production'), undefined)
  assert.ok(open(site, 'test'))
  event('打开 ' + site + '，部署名 RedLane')
  assert.equal(open(site, 'RedLane'), undefined)
  assert.ok(open(site, 'Red'))
})

test('显式登录意图与旧单例配置保持绑定，不把新站或模型文字转成旧账号登录', () => {
  const { event, guard, session } = host()
  // 旧入口调用没有站点参数，因此其授权只能来自当前真人消息。
  const login = () => guard({ name: 'mcp__atlas__login_site', arguments: {}, agent: { session } })
  assert.ok(login())
  event('登录测试后台')
  assert.equal(login(), undefined)
  event(site)
  assert.ok(login())
  event('登录本机配置的测试站点 ' + site)
  assert.ok(login())
  event('登录这个站')
  assert.ok(login())
  event('登录测试后台')
  assert.ok(login())
  event('请登录本机配置的测试站点')
  assert.equal(login(), undefined)
  event('这是登录本机配置的站点吗？')
  assert.ok(login())
  event('请用本机预先配置的账号登录。')
  assert.equal(login(), undefined)
  event('不用登录，告诉我页面标题。')
  event('登录本机配置的测试站点', { kind: 'injected' })
  assert.ok(login())
})

test('宿主重启后沿历史投影辨认新站，原站不可通过泛指的登录指令触发', () => {
  const { event, guard, session } = host()
  session.firstLiveSeq = 3
  session.deriveMessages = () => [{ role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: site }] }]
  event('登录测试后台')
  assert.ok(guard({name: 'mcp__atlas__login_site', arguments: {}, agent: {session}}))
  event('登录预先配置的后台')
  assert.equal(guard({name: 'mcp__atlas__login_site', arguments: {}, agent: {session}}), undefined)
})

test('网页、模型、注入的上下文、其他会话以及上一轮都不能授予新的访问地址', () => {
  const { event, open, guard } = host()
  assert.equal(guard({ name: 'mcp__atlas__get_run' }), undefined)
  event(site, { kind: 'user' })
  assert.ok(open(site))
  event(site, { kind: 'user', rpcId: 'fixture', principal: 'external-peer' })
  assert.ok(open(site))
  event(site, { kind: 'user', rpcId: 'fixture' }, 'assistant/message')
  event(site, { kind: 'user', rpcId: 'fixture' }, 'tool/result')
  event(site, { kind: 'injected' })
  assert.ok(open(site))
  event(site)
  assert.equal(open(site), undefined)
  assert.ok(open(site, undefined, {}))
  event('https://other.example', { kind: 'injected' })
  assert.ok(open('https://other.example'))
  event('继续')
  assert.ok(open(site))
  event(site)
  event('', {}, 'turn/end')
  assert.ok(open(site))
  event(site)
  event('', {}, 'turn/start')
  assert.ok(open(site))
})

test('精确识别网址边界，拒绝嵌在其他地址中的字符串及可能携带凭据的网址', () => {
  const { event, open } = host()
  event('[查看公开页](' + site + ')')
  assert.equal(open(site), undefined)
  event('[' + site + '](' + site + ')')
  assert.equal(open(site), undefined)
  event('[https://other.example](' + site + ')')
  assert.ok(open(site))
  assert.ok(open('https://other.example'))
  event('请打开 ' + site + '#/dashboard。')
  assert.equal(open(site + '#/dashboard'), undefined)
  assert.ok(open(site))
  assert.ok(open(site + '#/settings'))
  event('请打开 ' + site + '/../public。')
  assert.ok(open('https://public.example/public'))
  for (const address of [site + '?next=https://other.example', site + '#token', 'https://demo:example@public.example']) {
    event('打开 ' + address)
    assert.ok(open(address))
    assert.ok(open(site))
    assert.ok(open('https://other.example'))
  }
  event(site + ' 和 https://other.example，这是测试环境还是生产环境？')
  assert.ok(open(site, '测试'))
})

const openSite = 'mcp__atlas__open_site'
const updateSite = 'mcp__atlas__update_site_address'
const ask = 'ask_user_question'
const named = { site_name: '百度' }
const update = { ...named, url: 'https://new.example/' }
const ambiguous = { ...named, status: 'ambiguous', environments: ['生产环境', '测试环境'] }
const question = { questions: [{ id: 'environment', question: '要更新百度的哪个环境？',
  options: ambiguous.environments.map(label => ({ label })) }] }

test('裸域名可绑定原文的站点名，按名称访问和显式改址使用各自的入口', () => {
  const { event, dispatch } = host()
  event('这是百度new.example，这是测试环境')
  assert.equal(dispatch(openSite, { ...update, environment: '测试' }), undefined)
  assert.ok(dispatch(openSite, { ...update, site_name: 'new', environment: '测试' }))
  assert.ok(dispatch(openSite, { ...update, site_name: 'Aweb', environment: '测试' }))
  assert.ok(dispatch(updateSite, { ...update, environment: '测试' }))
  event('请访问百度测试环境')
  assert.equal(dispatch(openSite, { ...named, environment: '测试环境' }), undefined)
  assert.ok(dispatch(openSite, { ...update, environment: '测试环境' }))
  assert.ok(dispatch(openSite, { ...named, environment: '生产环境' }))
  event('把百度地址更新为 new.example')
  assert.equal(dispatch(updateSite, update), undefined)
  assert.ok(dispatch(updateSite, { ...update, environment: '未指定' }))
  assert.ok(dispatch(updateSite, { ...update, url: 'https://wrong.example/' }))
  assert.ok(dispatch(openSite, update))
  assert.ok(dispatch(openSite, { url: update.url }))
  event('把 Aweb 地址更新为 new.example，顺便看看 Bweb')
  assert.equal(dispatch(updateSite, { ...update, site_name: 'Aweb' }), undefined)
  assert.ok(dispatch(updateSite, { ...update, site_name: 'Bweb' }))
  event('把百度地图地址更新为 new.example')
  assert.ok(dispatch(updateSite, update))
  assert.ok(dispatch(updateSite, { ...update, site_name: '地图' }))
  assert.equal(dispatch(updateSite, { ...update, site_name: '百度地图' }), undefined)
  event('把百度换成 new.example')
  assert.equal(dispatch(updateSite, update), undefined)
  event('修改百度文章，素材见 new.example')
  assert.ok(dispatch(updateSite, update))
  event('把百度的产品文案更新成 new.example')
  assert.ok(dispatch(updateSite, update))
  event('不要把百度地址更新为 new.example')
  assert.ok(dispatch(updateSite, update))
  assert.ok(dispatch(openSite, update))
  for (const token of ['user@new.example', 'new.example?next=example.org', 'ftp://new.example', 'other.example/path/new.example']) {
    event('这是百度 ' + token)
    assert.ok(dispatch(openSite, update))
  }
})

test('环境卡片只接受实际候选标签，真人选中后仅重试原操作、站点、网址和环境一次', () => {
  const { event, dispatch, result } = host()
  event('把百度地址更新为 new.example')
  assert.equal(dispatch(updateSite, update, 'resolve'), undefined)
  result('resolve', ambiguous)
  assert.ok(dispatch(updateSite, { ...update, environment: '测试环境' }))
  assert.ok(dispatch(ask, { questions: [{ ...question.questions[0], options: [{ label: '开发环境' }] }] }, 'wrong'))
  assert.equal(dispatch(ask, question, 'choose'), undefined)
  assert.ok(dispatch(ask, question, 'second'))
  result('fake', { answers: [{ id: 'environment', selected: ['生产环境'] }] })
  assert.ok(dispatch(updateSite, { ...update, environment: '生产环境' }))
  result('choose', { answers: [{ id: 'environment', selected: ['测试环境'] }] })
  assert.ok(dispatch(openSite, { ...update, environment: '测试环境' }))
  assert.ok(dispatch(updateSite, { ...update, environment: '生产环境' }))
  assert.ok(dispatch(updateSite, { ...update, site_name: '另一个', environment: '测试环境' }))
  assert.ok(dispatch(updateSite, { ...update, url: 'other.example', environment: '测试环境' }))
  assert.equal(dispatch(updateSite, { ...update, environment: '测试环境' }, 'retry'), undefined)
  assert.ok(dispatch(updateSite, { ...update, environment: '测试环境' }))
})

test('跳过选择不改址，其他会话及穿过无关消息的旧选择不能授权；紧邻的文字回答可恢复原任务', () => {
  const { event, dispatch, result } = host()
  // 先模拟存储层返回两个已登记的环境。
  function begin() {
    event('把百度地址更新为 new.example')
    assert.equal(dispatch(updateSite, update, 'resolve'), undefined)
    result('resolve', ambiguous)
  }
  const selected = { ...update, environment: '生产环境' }
  begin()
  assert.equal(dispatch(ask, question, 'ask'), undefined)
  result('ask', { answers: [{ id: 'environment', selected: [] }] })
  assert.ok(dispatch(updateSite, selected))
  event('生产环境')
  assert.ok(dispatch(updateSite, selected))
  begin()
  event('', {}, 'turn/end')
  const another = {}
  event('生产环境', { kind: 'user', rpcId: 'other' }, 'user/message', another)
  assert.ok(dispatch(updateSite, selected, 'other', another))
  event('选生产环境')
  assert.equal(dispatch(updateSite, selected, 'retry'), undefined)
  begin()
  event('帮我看别的任务')
  event('生产环境')
  assert.ok(dispatch(updateSite, selected))
})

test('仅紧邻的同站点指令能沿用刚明确使用的环境', () => {
  const { event, dispatch, result } = host()
  event('打开百度测试环境')
  assert.equal(dispatch(openSite, { ...named, environment: '测试环境' }, 'read'), undefined)
  result('read', { status: 'read', run_id: 'synthetic', deployment: { environment: '测试环境' } })
  event('把百度地址换成 new.example')
  assert.match(dispatch(updateSite, update), /environment="测试环境"/u)
  assert.ok(dispatch(updateSite, { ...update, environment: '生产环境' }))
  assert.equal(dispatch(updateSite, { ...update, environment: '测试环境' }, 'write'), undefined)
  result('write', { ...named, status: 'updated', environment: '测试环境' })
  event('打开百度')
  assert.match(dispatch(openSite, named), /environment="测试环境"/u)
  event('介绍一下工具')
  event('把百度地址换成 new.example')
  assert.ok(dispatch(updateSite, { ...update, environment: '测试环境' }))
})
