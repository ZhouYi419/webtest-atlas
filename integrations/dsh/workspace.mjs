export const inject = ['workspaceRegistry', 'agentPresets', 'tools']

// 使用宿主公开服务登记工作区并检查预设的实际工具目录，全程无模型请求。
export async function apply(ctx) {
  await ctx.workspaceRegistry.create(process.env.DSH_CWD)
  // 默认解析与原生“新会话”采用同一规则，可及时发现外部模型设置误带入的个人预设。
  const preset = await ctx.agentPresets.resolve()
  if (preset.id !== 'atlas') {
    throw new Error('DSH 新对话须使用本项目的 Atlas 预设。')
  }
  // 临时持有当前预设版本，按该作用域查询模型实际可见的工具，检查完即释放。
  const scope = await ctx.agentPresets.acquireScope(preset.id)
  try {
    const names = ctx.tools.schemas(scope.key).map(schema => schema.name).sort()
    const expected = [
      'ask_user_question', 'mcp__atlas__get_run', 'mcp__atlas__login_site',
      'mcp__atlas__open_site', 'mcp__atlas__read_demo_page', 'mcp__atlas__update_site_address',
    ]
    if (names.length !== expected.length || names.some((name, index) => name !== expected[index])) {
      throw new Error(`Atlas 工具目录与预期不符，实际装载：${names.join(', ') || '空'}。`)
    }
    console.info('Atlas MCP:', names.join(', '))
  } finally {
    await scope[Symbol.asyncDispose]()
  }
  // 给 Web 必需入口提供就绪信号。校验抛错时，CLI 会释放插件树并以非零状态退出。
  ctx.provide('atlasReady', {})
}
