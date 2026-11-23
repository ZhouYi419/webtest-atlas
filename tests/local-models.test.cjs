// 用 rc.5 自带的解析器和临时 Web 配置验证项目专用模型映射。ATLAS_DSH_CLI 指向该版本的 bin.js。
const assert = require('node:assert/strict')
const { mkdtempSync, readFileSync, rmdirSync, unlinkSync, writeFileSync } = require('node:fs')
const { tmpdir } = require('node:os')
const { join } = require('node:path')
const test = require('node:test')

test('仅把 DeepSeek 官方端点的旧 Flash 选择迁移到项目新模型 ID', () => {
  const home = mkdtempSync(join(tmpdir(), 'atlas-model-id-'))
  const settings = join(home, 'settings.yaml')
  const reader = require.resolve('../integrations/dsh/local-models.cjs')
  const originalHome = process.env.ATLAS_MODEL_HOME
  const originalBaseUrl = process.env.DEEPSEEK_BASE_URL
  process.env.ATLAS_MODEL_HOME = home
  delete process.env.DEEPSEEK_BASE_URL

  // 在同一临时目录模拟一次用户配置和一次独立宿主启动，核对读取过程中原件内容保持一致。
  function select(provider, model, adapter = {}) {
    const data = JSON.stringify({
      'agent-default-model': { provider, model, reasoningEffort: 'low' },
      'llm-deepseek': adapter,
    })
    writeFileSync(settings, data)
    delete require.cache[reader]
    const selected = require(reader)['agent-default-model']
    assert.equal(readFileSync(settings, 'utf8'), data)
    return selected
  }

  try {
    assert.deepEqual(select('deepseek-official', 'deepseek-v4-flash'), {
      provider: 'deepseek-official', model: 'deepseek-flash', reasoningEffort: 'low',
    })
    assert.equal(select('deepseek-official', 'deepseek-v4-pro').model, 'deepseek-v4-pro')
    assert.equal(select('private-gateway', 'deepseek-v4-flash').model, 'deepseek-v4-flash')
    const gateway = { baseURL: 'https://gateway.invalid/anthropic' }
    assert.equal(select('deepseek-official', 'deepseek-v4-flash', gateway).model, 'deepseek-v4-flash')
    process.env.DEEPSEEK_BASE_URL = gateway.baseURL
    assert.equal(select('deepseek-official', 'deepseek-v4-flash').model, 'deepseek-v4-flash')
  } finally {
    if (originalHome === undefined) delete process.env.ATLAS_MODEL_HOME
    else process.env.ATLAS_MODEL_HOME = originalHome
    if (originalBaseUrl === undefined) delete process.env.DEEPSEEK_BASE_URL
    else process.env.DEEPSEEK_BASE_URL = originalBaseUrl
    unlinkSync(settings)
    rmdirSync(home)
  }
})
