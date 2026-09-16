// 计划卡片：deepseek-harness 计划模式（plan mode）的「页面附着」呈现。
//
// 运行时通过 exit_plan_mode 提交计划后会被评审通道阻塞，后端把计划作为
// kind='plan' 的过程节点推来。这里以卡片附着在回答上方：不弹窗、不打断对话流，
// 用户就地确认「批准并执行 / 继续规划」，结论由页面回传给后端交给运行时。
Component({
  properties: {
    node: { type: Object, value: null as any },
    messageId: { type: String, value: '' },
    open: { type: Boolean, value: true },
  },
  methods: {
    onToggle() {
      this.triggerEvent('toggle', { messageId: this.data.messageId })
    },
    onApprove() {
      this.submit(true)
    },
    onRevise() {
      this.submit(false)
    },
    submit(approved: boolean) {
      const node: any = this.data.node || {}
      // 只在「待确认」时可提交：已批准/已提交/评审超时都不允许重复回写
      if (node.reviewState !== 'review') return
      this.triggerEvent('review', {
        messageId: this.data.messageId,
        reviewId: node.reviewId || '',
        approved,
      })
    },
  },
})
