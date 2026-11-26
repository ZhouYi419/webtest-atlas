// 由 DSH 加载到本项目的原生会话，在可以打字的位置标出当前支持的输入。
window.__ModuleLoader__.load({
  id: 'webtest-atlas-site-entry',
  factory(require) {
    const { createElement } = require('react')

    // 空白及已有会话共用输入框上方的插槽，随宿主配色和字体显示。
    function SiteEntryHint() {
      return createElement('div', {
        'data-atlas-site-entry-hint': '',
        style: {
          boxSizing: 'border-box', width: '100%', maxWidth: 'var(--dsh-composer-card-max-width)',
          marginInline: 'auto', padding: '0.35em 1em', fontSize: '0.85em', lineHeight: 1.5,
        },
      }, '发网址可查看首页，给站点起名后可按名称打开或改地址。请勿输入账号密码。')
    }

    return {
      inject: ['slots'],
      // 插槽出现时注册，其他会话组件继续使用宿主原有的编辑和发送逻辑。
      apply(ctx) {
        ctx.slots.inject('conversation.input.dock', () => ctx.slots.register({
          name: 'conversation.input.dock', id: 'atlas-site-entry-hint', order: 10,
        }, SiteEntryHint))
      },
    }
  },
})
