// ATLAS_DSH_CLI 指向项目锁定版本的 bin.js，使用该宿主的真实补丁解析器。
const assert = require('node:assert/strict')
const { mkdirSync, mkdtempSync, rmSync, writeFileSync } = require('node:fs')
const { tmpdir } = require('node:os')
const { join } = require('node:path')
const test = require('node:test')

test('按宿主默认值、Web profile、主目录的顺序读取静态模型补丁', t => {
  const home = mkdtempSync(join(tmpdir(), 'atlas-models-'))
  const reader = require.resolve('../integrations/dsh/local-models.cjs')
  const originalHome = process.env.ATLAS_MODEL_HOME
  process.env.ATLAS_MODEL_HOME = home
  t.after(() => {
    delete require.cache[reader]
    if (originalHome === undefined) delete process.env.ATLAS_MODEL_HOME
    else process.env.ATLAS_MODEL_HOME = originalHome
    rmSync(home, { recursive: true, force: true })
  })

  // 模拟重启，让每次断言读取刚写入的补丁。
  function loadModels() {
    delete require.cache[reader]
    return require(reader)
  }

  assert.deepEqual(loadModels()['agent-default-model'], {
    provider: 'deepseek-official', model: 'deepseek-flash',
  })
  const web = join(home, 'profiles', 'web')
  mkdirSync(web, { recursive: true })
  const selection = { provider: 'private-gateway', model: 'team-model', reasoningEffort: 'low' }
  writeFileSync(join(web, 'cordis.patch.yml'), JSON.stringify([
    { id: 'agent-default-model', config: selection },
    { id: 'llm-pi-ai', disabled: true },
  ]))
  assert.deepEqual(loadModels()['agent-default-model'], selection)
  const shared = join(home, 'cordis.patch.yml')
  const override = { provider: 'deepseek-official', model: 'deepseek-pro' }
  writeFileSync(shared, JSON.stringify([{ id: 'agent-default-model', config: override }]))
  const models = loadModels()
  assert.deepEqual(models['agent-default-model'], override)
  assert.equal(models.disabled['llm-pi-ai'], true)
  writeFileSync(shared, '- id: agent-default-model\n  config: !!js ({})\n')
  assert.throws(loadModels, /动态 JavaScript 表达式/)
})
