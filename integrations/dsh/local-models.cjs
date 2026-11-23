// 在宿主进程内只读投影本机 Web 的模型设置。模块缓存让同次启动的各适配器读取同一快照。
const { readFileSync } = require('node:fs')
const { createRequire } = require('node:module')
const { join } = require('node:path')

const fromDsh = createRequire(process.env.ATLAS_DSH_CLI)
const { applyEntryPatches, entryListSchema } = fromDsh('@deepseek-ai/cordis-plugin-include')
const yaml = fromDsh('js-yaml')
const modelIds = ['agent-default-model', 'llm-deepseek', 'llm-pi-ai', 'llm-deepseek-account']

// 允许缺省本地覆盖。解析失败时不把 YAML 片段或其中的配置值写入宿主日志。
function readYaml(path, fallback) {
  try {
    return yaml.load(readFileSync(path, 'utf8'), { schema: entryListSchema }) ?? fallback
  } catch (error) {
    if (error.code === 'ENOENT' && fallback !== undefined) return fallback
    throw new Error(`无法读取 DSH 模型设置来源 ${path}，请检查文件格式和访问权限。`)
  }
}

// 对应旧版 settings.yaml 的字段级导入。对象递归合并，数组及标量整项替换。
function mergeLegacy(base, value) {
  if (!base || typeof base !== 'object' || Array.isArray(base) ||
      !value || typeof value !== 'object' || Array.isArray(value)) return value
  return Object.fromEntries([
    ...Object.entries(base),
    ...Object.entries(value).map(([key, item]) => [key, mergeLegacy(base[key], item)]),
  ])
}

// 仅接受模型设置的数据字段。其他预设、个人插件和配置脚本不会挂载进 Atlas。
function requireData(value) {
  if (!value || typeof value !== 'object') return
  if (Object.hasOwn(value, '__jsExpr')) {
    throw new Error('复用的模型设置包含动态 JavaScript 表达式，请在项目 profile 中显式配置该模型。')
  }
  Object.values(value).forEach(requireData)
}

const home = process.env.ATLAS_MODEL_HOME
const bundles = ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app']
const defaults = bundles.flatMap(name => readYaml(fromDsh.resolve(name + '/cordis.patch.yml')))
const web = readYaml(join(home, 'profiles', 'web', 'cordis.patch.yml'), [])
const shared = readYaml(join(home, 'cordis.patch.yml'), [])
const legacy = readYaml(join(home, 'settings.yaml'), {})
const profileRows = applyEntryPatches([], [...defaults, ...web], () => {})
const imports = []
for (const id of modelIds) {
  if (Object.hasOwn(legacy, id)) {
    const row = profileRows.find(entry => entry.id === id)
    imports.push({ id, config: mergeLegacy(row?.config ?? {}, legacy[id]) })
  }
}
const rows = applyEntryPatches(profileRows, [...imports, ...shared], () => {})
const modelConfigs = { disabled: {} }
for (const id of modelIds) {
  const row = rows.find(entry => entry.id === id)
  const config = row?.config ?? {}
  requireData(config)
  requireData(row?.disabled)
  modelConfigs[id] = config
  modelConfigs.disabled[id] = Boolean(row?.disabled)
}

// 本项目在官方端点沿用 Flash 系列。第三方网关及其他已保存选择沿用各自的模型标识。
const selection = modelConfigs['agent-default-model']
const officialUrl = 'https://api.deepseek.com/anthropic'
const baseUrl = modelConfigs['llm-deepseek'].baseURL ?? process.env.DEEPSEEK_BASE_URL ?? officialUrl
if (selection.provider === 'deepseek-official' && selection.model === 'deepseek-v4-flash' &&
    baseUrl.replace(/\/+$/, '') === officialUrl) {
  modelConfigs['agent-default-model'] = { ...selection, model: 'deepseek-flash' }
}
module.exports = modelConfigs
