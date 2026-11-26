// 只在正式宿主分派公开页面读取前，使用当前会话真实用户消息核对目标。
export const inject = ['tools', 'sessions']

const openSite = 'mcp__atlas__open_site'
const updateSite = 'mcp__atlas__update_site_address'
const ask = 'ask_user_question'
const loginSite = 'mcp__atlas__login_site'
const denied = '操作与本次用户提供的站点名称、地址或环境不符。请按原话选择工具和参数。'
const choose = '请调用 ask_user_question，使用 id 为 environment 的单选题，选项标签逐一使用返回的原始环境名称。'
const deniedLogin = '该登录操作只用于本机预先配置的站点。请明确是否要登录该配置的站点。'
const unlabelled = '未指定'
const shortAliases = new Set(['生产', '测试', '开发', '预发'])

// 用同源及原始完整路径比对，保留 Vue 的 #/ 路由。路径细节交给执行器校验。
function publicAddress(text) {
  if (typeof text !== 'string' || /[\\?\s\u0000-\u001f\u007f]/u.test(text)) return
  if (/^(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]*(?::\d+)?(?:[/#].*)?$/iu.test(text)) text = 'https://' + text
  const parts = /^https?:\/\/([^/#]+)(\/[^#]*)?(#\/[^#]*)?$/iu.exec(text)
  if (!parts || /[@%]/u.test(parts[1])) return
  try {
    const url = new URL(text)
    if (url.port === '0') return
    return url.origin + (parts[2] || '/') + (parts[3] || '')
  } catch {
    return
  }
}

// 保留用户给出的环境名，仅合并带“环境”后缀的常用标签。
function environmentName(text) {
  const label = text.trim()
  const short = label.replace(/环境$/u, '')
  return shortAliases.has(short) ? short : label
}

// 先提取清楚的命名语句，再由工具提出的标签与用户原文核对。“所在环境”只是在询问结果。
function environmentAllowed(text, value) {
  if (typeof value !== 'string' || !/^[\p{L}\p{N}_ -]{1,40}$/u.test(value)) return false
  const names = new Set()
  // “这是沙箱环境”“Aweb 的压测环境”“QA 环境”均能按用户自己的名字定位。
  for (const match of text.matchAll(/([\p{L}\p{N}_-]{1,40})\s*环境/gu)) {
    const prefix = match[1]
    if (/(?:所在|当前|对应|目标|哪个|该|本次|各自|运行|所在的|对应的)$/u.test(prefix)) continue
    const label = prefix.split(/(?:这是|属于|作为|的|是|为)/u).at(-1)
    if (label && !/^(?:用|看|请|到|新|在|或|和|查|名称|本|其)$/u.test(label)) names.add(environmentName(label + '环境'))
  }
  for (const match of text.matchAll(/(测试|生产|开发|预发)用的/gu)) names.add(match[1])
  // 引号内可以有空格。显式命名优先于从普通散文中猜测，“test”本身也可能是动词。
  for (const match of text.matchAll(/环境(?:名称|名)?\s*(?:[:：=]|为|是|叫做|叫)\s*(?:[“「"']([\p{L}\p{N}_ -]{1,40})[”」"']|([\p{L}\p{N}_-]+))/gu)) {
    names.add(environmentName(match[1] ?? match[2]))
  }
  const wanted = environmentName(value)
  if (names.size) return names.size === 1 && (names.has(wanted) || names.has(wanted + '环境'))
  // 这些短名称常单独表示部署。其他名字同样可用，只要原文给出了相同文字。
  const standalone = text.match(/\b(?:QA|UAT|staging|production|prod)\b/giu) ?? []
  if (standalone.length) return standalone.every(label => label === value.trim())
  if (wanted === unlabelled) return true
  const label = value.trim()
  return new RegExp('(?:部署|环境)(?:名(?:称)?)?\\s*(?:[:：=]|为|是|叫)?\\s*' + label + '(?![A-Za-z0-9_-])', 'u').test(text)
}

// 仅从当前用户消息里抽取可访问的完整网址；环境核验时排除网址中的主机名和路径。
function messageTarget(words) {
  const urls = new Set()
  const links = /\[([^\[\]\n]*)\]\(([^\s()]+)\)|[a-z][a-z0-9+.-]*:\/\/[^\s<>"'`，。！？；、（）【】《》“”‘’]+|(?<![a-z0-9_./:@%=+-])(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]*(?::\d+)?(?:[/?#@%\\][^\s<>"'`，。！？；、（）【】《》“”‘’]*)?/giu
  const description = words.replace(links, (whole, label, destination, offset) => {
    let token = destination ?? whole
    // Markdown 链接及常见包裹符号中的结束括号属于外部排版。
    if (!destination && words[offset - 1] === '(' && token.endsWith(')')) token = token.slice(0, -1)
    const address = publicAddress(token)
    const addressLabel = label && (publicAddress(label) || /:\/\/|@/u.test(label))
    if (address && (!addressLabel || publicAddress(label) === address)) urls.add(address)
    const visibleName = label && !addressLabel ? ' ' + label + ' ' : ' '
    // 保留网址的语法位置，让“把百度换成〈地址〉”能与“修改百度文章，参考〈地址〉”区分。
    return address ? visibleName + '\uFFFC ' : visibleName
  })
  return { urls, description }
}

// 纯文字仅来自当前会话的真实用户消息，表单、工具返回和页面内容都不参与授予权限。
function messageText(content) {
  return content.filter(block => block.type === 'text').map(block => block.text).join('\n')
}

// 单例配置只接受明确的登录请求。对话正在讨论某个网址时，需要用户直接指明本机配置。
function configuredLoginAllowed(words, hasSiteContext) {
  if (/https?:\/\//iu.test(words)) return false
  const request = words.trim().replace(/^(?:请帮我|请|帮我|麻烦|现在)\s*/u, '')
  const configured = '(?:本机(?:已|预先)?配置|预先配置|已在本机配置)'
  const target = '(?:的)?(?:测试)?(?:后台|站点|网站|环境|站)'
  if (new RegExp('^(?:(?:重新|恢复)?登录' + configured + target + '|(?:用|使用|按)' + configured
    + '(?:' + target + '|的账号|的凭据)?(?:重新)?登录)(?:$|[。！!，,\\s])', 'u').test(request)) return true
  return !hasSiteContext && /^(?:重新|恢复)?登录测试后台(?:$|[。！!，,\s])/u.test(request)
}

// 中文站名没有天然分词边界；紧邻汉字必须属于指令或站点限定语，防止把“百度地图”截为“百度”。
function namedIn(text, name) {
  if (typeof name !== 'string' || !/^[\p{L}\p{N}_][\p{L}\p{N}_. -]{0,63}$/u.test(name) || name !== name.trim()) return false
  const literal = new RegExp(name.replaceAll('.', '\\.'), 'giu')
  for (const match of text.matchAll(literal)) {
    const before = text.slice(0, match.index)
    const after = text.slice(match.index + name.length)
    const start = !/[\p{L}\p{N}_-]$/u.test(before) || /(?:把|将|给|对|叫|叫做|名为|是|的|在|用|看|开|测|问|读|到|查|进|下|试|站点|网站|记住|保存|绑定|修改|更新|更换|替换|了解|理解|以及|还有|和|与)$/u.test(before)
    const end = !/^[\p{L}\p{N}_-]/u.test(after) || /^(?:的|这个|这边|是|叫|叫做|地址|网址|网站|站点|入口|域名|环境|更新|修改|换|改|和|与|以及|(?:测试|生产|开发|预发|沙箱|压测)环境)/u.test(after)
    if (start && end) return true
  }
  return false
}

// 有改址措辞时只能走配置更新；否定、假设和讨论更新方法的发言也不能发起写入。
function changeIntent(text) {
  text = text.replace(/更新(?:日志|记录|说明|时间|日期)|最近更新/gu, '')
  if (!/[改换](?:成|为|到|一下|下)|(?:更新|修改|更换|替换|变更)|[改换].{0,16}(?:地址|网址|域名|入口)|\b(?:change|update|replace)\b/iu.test(text)) return 'read'
  return /(?:不(?:要|用|必|能|想|会|应|再|打算|准备|更新|修改|替换|换|改|把|将)|无需|暂不|先不|别|如果|假如|假设|怎么|如何|是否|可以.{0,40}吗|[?？]|\b(?:if|how|don't|do not)\b)/iu.test(text) ? 'discussion' : 'update'
}

// 确认变更对象是该站点的入口，网址仅作为素材出现在另一个请求时不能改配置。
function addressChangeFor(text, siteName) {
  return text.split(/[，,。；;\n]/u).some(clause => {
    if (!namedIn(clause, siteName)) return false
    const target = clause.replace(new RegExp(siteName.replaceAll('.', '\\.'), 'giu'), '●')
    const explicit = /(?:地址|网址|入口|域名|\b(?:address|url|domain)\b)/iu
    if (explicit.test(target)) return changeIntent(clause) === 'update' || /(?:地址|网址|入口|域名)\s*$/u.test(target)
    return /●\s*(?:的)?(?:[\p{L}\p{N}_-]{1,40}环境)?\s*(?:改|换|更新|修改|更换|替换|变更)(?:到|为|成)\s*\uFFFC/u.test(target)
      || /(?:更新|修改|更换|替换|变更|\b(?:update|change|replace)\b)\s*●\s*(?:到|为|成|to|with)\s*\uFFFC/iu.test(target)
  })
}

// 从当前指令构造允许分派的操作；按名称读取时绝不让模型补网址。
function authorizedSite(grant, name, args) {
  const target = grant.target
  const intent = changeIntent(target.description)
  const siteName = args.site_name ?? null
  const url = args.url == null ? null : publicAddress(args.url)
  if (args.url != null && !url) return false
  if (siteName !== null && !namedIn(target.description, siteName)) return false
  if (name === updateSite && (intent !== 'update' || !siteName || !url || target.urls.size !== 1)) return false
  if (name === updateSite && !addressChangeFor(target.description, siteName)) return false
  if (name === openSite && intent !== 'read') return false
  if (url ? !target.urls.has(url) : target.urls.size || !siteName) return false
  if (!url && !/(?:打开|看看|访问|读取|测|查看|检查|看一下|看下|进入|了解|理解|\b(?:open|read|visit|test|inspect)\b)/iu.test(target.description)) return false
  const label = args.environment ?? unlabelled
  const description = siteName ? target.description.replace(new RegExp(siteName.replaceAll('.', '\\.'), 'giu'), ' ') : target.description
  // “未指定”本身也可能是已保存的环境；用户没有指定时必须省略，交由存储层判断是否需要追问。
  if (siteName && args.environment === unlabelled && !description.includes(unlabelled)) return false
  // 上一条真人指令明确使用的同名环境，可以在紧邻的指令中沿用；新消息中明确的环境优先。
  if (siteName && grant.prior?.siteName === siteName && !description.includes(unlabelled) && environmentAllowed(description, unlabelled)
      && args.environment != null && environmentName(grant.prior.environment) === environmentName(label)) return true
  return environmentAllowed(description, label)
}

// 卡片只开放该次查询实际返回的全部环境，避免问题显示与写入的对象或环境不一致。
function matchesQuestion(pending, questions) {
  if (!pending || pending.answered || pending.asking || !Array.isArray(questions) || questions.length !== 1) return false
  const [question] = questions
  if (question?.id !== 'environment' || question.multi_select || !Array.isArray(question.options)) return false
  const options = new Set(question.options.map(option => option?.label))
  return question.options.length === pending.labels.length && pending.labels.every(label => options.has(label))
}

// 真人可在紧接着的消息里直接回复唯一候选环境；普通新任务不会继承待选操作。
function replyChoice(words, labels) {
  const reply = words.trim().replace(/^(?:我选(?:择)?|选(?:择)?|用|就用)\s*/u, '').replace(/[。！!]$/u, '').trim()
  return labels.find(label => environmentName(reply) === environmentName(label))
}

// 通过已授权分派的 callId 识别后端结构化结果，与网页标题、助手文本及其他工具输出隔离。
function siteResult(state, event) {
  const message = event.data.message
  const call = state.grant?.calls.get(message?.toolCallId)
  if (!call) return
  state.grant.calls.delete(message.toolCallId)
  const pending = state.pending
  if (call.name === ask) {
    if (!pending || call.pending !== pending) return
    pending.asking = false
    pending.answered = true
  }
  if (event.data.error !== undefined || message.isError) return
  const text = message.content?.find(part => part.type === 'text')?.text
  let result
  try { result = JSON.parse(text) } catch { return }
  if (!result || typeof result !== 'object') return
  if (call.name === ask) {
    const answer = result.answers?.find(item => item.id === 'environment')
    if (answer?.selected?.length === 1 && !answer.custom && pending.labels.includes(answer.selected[0])) {
      pending.selected = answer.selected[0]
    }
    return
  }
  if (!call.siteName) return
  if (result.status === 'ambiguous' && typeof result.site_name === 'string'
      && result.site_name.toLowerCase() === call.siteName.toLowerCase()
      && Array.isArray(result.environments) && result.environments.length > 1
      && result.environments.every(label => typeof label === 'string' && label.length > 0)) {
    state.pending = { ...call, labels: result.environments, answered: false, asking: false }
    return
  }
  const environment = result.deployment?.environment ?? result.environment
  if (call.environment && typeof environment === 'string' && environmentName(environment) === environmentName(call.environment)
      && (result.run_id || result.status === 'updated')) state.last = { siteName: call.siteName, environment }
}

// 原生 Web 的 user/message 带有由会话控制器写入的用户来源和 requestId；网页、模型及回放均不能登记。
export function apply(ctx) {
  const sessions = new WeakMap()
  const siteConversations = new WeakSet()
  ctx.on('session/event', (session, event) => {
    const state = sessions.get(session) ?? {}
    sessions.set(session, state)
    if (event.type === 'turn/start' || event.type === 'turn/end') {
      delete state.grant
      return
    }
    if (event.type === 'tool/result') return siteResult(state, event)
    if (event.type !== 'user/message' || event.data.source.kind !== 'user') return
    delete state.grant
    const { source, content } = event.data
    if (typeof source.rpcId !== 'string' || !source.rpcId || source.principal !== undefined) {
      delete state.pending
      delete state.last
      return
    }
    const words = messageText(content)
    const target = messageTarget(words)
    if (target.urls.size) siteConversations.add(session)
    const reply = state.pending && !state.pending.answered && replyChoice(words, state.pending.labels)
    if (reply) {
      state.pending.selected = reply
      state.pending.answered = true
    } else {
      delete state.pending
    }
    state.grant = { words, target, calls: new Map(), prior: state.last }
    delete state.last
  })

  // DSH 的单调守卫位于所有可扩展预执行策略之后，其拒绝结果不会再进入 MCP 转发。
  ctx.tools.guard(execution => {
    const { name, arguments: args } = execution
    if (![openSite, updateSite, loginSite, ask].includes(name)) return
    const session = execution.agent?.session
    const state = sessions.get(session)
    const grant = state?.grant
    if (!grant) return name === loginSite ? deniedLogin : denied
    if (name === loginSite) {
      // 恢复或分叉的对话从 DSH 当前历史投影核对已有站点上下文，避免进程重启后忘记隔离。
      const priorSite = siteConversations.has(session) || (session.firstLiveSeq > 0
        && session.deriveMessages().some(message => message.role === 'user' && message.source.kind === 'user'
          && messageTarget(messageText(message.content)).urls.size > 0
          || message.role === 'assistant' && message.content.some(part => part.type === 'tool-call' && [openSite, updateSite].includes(part.name))))
      return configuredLoginAllowed(grant.words, priorSite) && !grant.target.urls.size ? undefined : deniedLogin
    }
    const pending = state.pending
    if (name === ask) {
      if (!matchesQuestion(pending, args?.questions)) return choose
      pending.asking = true
      grant.calls.set(execution.callId, { name, pending })
      return
    }
    if (!args || (args.environment != null && typeof args.environment !== 'string')) return denied
    const call = { name, siteName: args.site_name ?? null, url: args.url == null ? null : publicAddress(args.url),
      environment: args.environment ?? null }
    if (pending) {
      if (!pending.selected) return pending.answered ? '已跳过环境选择，本次操作已停止。' : choose
      if (pending.name !== name || pending.siteName !== call.siteName || pending.url !== call.url
          || (args.url != null && !call.url) || call.environment !== pending.selected || pending.used) return denied
      pending.used = true
    } else {
      if (!authorizedSite(grant, name, args)) return denied
      // DSH 的守卫接收冻结参数。给模型返回确定的重试参数，避免先查出多环境再打扰用户。
      if (call.siteName && grant.prior?.siteName === call.siteName && !call.environment && (name === updateSite || !call.url)) {
        return '上轮已明确该站点的环境。请立即使用相同工具和参数重试，并填写 environment='
          + JSON.stringify(grant.prior.environment) + '。'
      }
    }
    siteConversations.add(session)
    if (execution.callId) grant.calls.set(execution.callId, call)
  })
  ctx.provide('atlasPublicInputGuard', {})
}
