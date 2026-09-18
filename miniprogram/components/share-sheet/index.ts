/**
 * 分享面板：把对话里勾选的内容与文件分享到微信好友 / QQ / 钉钉 / 知识库。
 *
 * 面板只管「选哪个去处」，真正的动作留给页面：
 *  · 微信好友是 open-type="share" 的按钮，转发的卡片内容由页面的 onShareAppMessage 决定
 *    （微信要求同步返回转发内容，页面的 payload 里已经带 promise 兜底）；
 *  · QQ / 钉钉微信不允许直接跳转，页面把内容复制到剪贴板，面板只负责把这件事写清楚；
 *  · 存到知识库要先选目标知识库，所以面板内有第二层「选知识库」。
 */
Component({
  properties: {
    visible: { type: Boolean, value: false },
    messageCount: { type: Number, value: 0 },
    fileCount: { type: Number, value: 0 },
    knowledges: { type: Array, value: [] },
    knowledgeLoading: { type: Boolean, value: false },
    hint: { type: String, value: '' },
  },
  data: {
    stage: 'targets',
  },
  observers: {
    visible(value: boolean) {
      if (!value) this.setData({ stage: 'targets' })
    },
  },
  methods: {
    noop() {},
    close() { this.triggerEvent('close') },
    // 微信好友：必须是 open-type="share" 的按钮，微信才会拉起转发面板
    onWechat() { this.triggerEvent('wechat') },
    onCopy(e: any) { this.triggerEvent('copy', { target: String((e.currentTarget.dataset || {}).target || '') }) },
    openKnowledge() {
      this.setData({ stage: 'knowledge' })
      this.triggerEvent('knowledgeopen')
    },
    back() { this.setData({ stage: 'targets' }) },
    pickKnowledge(e: any) {
      const dataset = e.currentTarget.dataset || {}
      this.triggerEvent('knowledgepick', { id: String(dataset.id || ''), name: String(dataset.name || '') })
    },
  },
})
