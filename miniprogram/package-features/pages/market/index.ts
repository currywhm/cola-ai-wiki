import { getMarketKnowledge, subscribeKnowledge, unsubscribeKnowledge } from '../../../services/api'

Page({
  data: { query: '', loading: false, items: [] as any[], category: '', error: '', busyId: '' },
  onShow() { this.load() },
  load() {
    this.setData({ loading: true, error: '' })
    getMarketKnowledge(this.data.query, this.data.category)
      .then((items) => this.setData({ items, loading: false }))
      .catch(() => this.setData({ items: [], loading: false, error: '知识库广场暂时无法连接，请稍后重试。' }))
  },
  onInput(e: any) { this.setData({ query: e.detail.value }) },
  search() { this.load() },
  chooseCategory(e: any) { this.setData({ category: e.currentTarget.dataset.category || '' }); this.load() },
  toggleSubscription(e: any) {
    const knowledgeId = String(e.currentTarget.dataset.id || '')
    const item = this.data.items.find((entry: any) => entry.id === knowledgeId)
    if (!knowledgeId || !item || item.owned || this.data.busyId) return
    this.setData({ busyId: knowledgeId })
    const request = item.subscribed ? unsubscribeKnowledge(knowledgeId) : subscribeKnowledge(knowledgeId)
    request.then((result) => {
      this.setData({
        busyId: '',
        items: this.data.items.map((entry: any) => entry.id === knowledgeId
          ? { ...entry, subscribed: !item.subscribed, subscribers: Number(result.subscribers || 0) }
          : entry),
      })
      wx.showToast({ title: item.subscribed ? '已取消订阅' : '订阅成功', icon: 'none' })
    }).catch((error: any) => {
      this.setData({ busyId: '' })
      wx.showToast({ title: error?.message || '操作失败，请稍后重试', icon: 'none' })
    })
  },
  backToChat() { wx.navigateBack() },
})
