/**
 * 对话结束后的多选操作条：取消 / 全选 … 已选 N 项 … 分享。
 *
 * 只用文字与一个主按钮，不加重底色：多选是临时状态，视觉上不能盖过对话本身。
 */
Component({
  properties: {
    count: { type: Number, value: 0 },
    total: { type: Number, value: 0 },
    fileCount: { type: Number, value: 0 },
  },
  methods: {
    onClose() { this.triggerEvent('close') },
    onAll() { this.triggerEvent('all') },
    onShare() { this.triggerEvent('share') },
  },
})
