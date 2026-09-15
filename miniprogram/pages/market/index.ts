import { getMarketKnowledge } from '../../services/api'

Page({
  data: { query: '', loading: false, items: [] as any[], category: '', error: '' },
  onShow() { this.load() },
  load() { this.setData({ loading: true, error: '' }); getMarketKnowledge(this.data.query, this.data.category).then(items => this.setData({ items, loading: false })).catch(() => this.setData({ items: [], loading: false, error: '知识库广场暂时无法连接，请稍后重试。' })) },
  onInput(e: any) { this.setData({ query: e.detail.value }) },
  search() { this.load() },
  chooseCategory(e: any) { this.setData({ category: e.currentTarget.dataset.category || '' }); this.load() },
  backToChat() { wx.navigateBack() },
})
