import { deleteKnowledge, getKnowledge, Knowledge } from '../../services/api'

Page({
  data: { knowledge: [] as Knowledge[], visibleKnowledge: [] as Knowledge[], sortMode: 'all', query: '', searchVisible: false, loadState: 'loading' },
  onShow() { this.load() },
  load() { this.setData({ loadState: 'loading' }); getKnowledge().then((knowledge) => { const view = knowledge.map((item) => ({ ...item, displayDate: item.updated_at ? item.updated_at.substring(0, 10) : '刚刚' })); this.setData({ knowledge: view, loadState: 'ready' }); this.applyFilter(view, this.data.sortMode) }).catch(() => this.setData({ loadState: 'error', knowledge: [], visibleKnowledge: [] })) },
  applyFilter(items: Knowledge[], mode: string) {
    const visible = items.filter(item => item.name.toLowerCase().includes(this.data.query.trim().toLowerCase()))
    if (mode === 'recent') visible.sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))
    if (mode === 'count') visible.sort((a, b) => (b.document_count || 0) - (a.document_count || 0))
    this.setData({ visibleKnowledge: visible })
  },
  filterInput(e: any) { this.setData({ query: e.detail.value }); this.applyFilter(this.data.knowledge, this.data.sortMode) },
  toggleSearch() { const next = !this.data.searchVisible; this.setData({ searchVisible: next, query: next ? this.data.query : '' }); if (!next) this.applyFilter(this.data.knowledge, this.data.sortMode) },
  filterAll() { this.setData({ sortMode: 'all' }); this.applyFilter(this.data.knowledge, 'all') },
  filterRecent() { this.setData({ sortMode: 'recent' }); this.applyFilter(this.data.knowledge, 'recent') },
  filterCount() { this.setData({ sortMode: 'count' }); this.applyFilter(this.data.knowledge, 'count') },
  open(e: any) { wx.navigateTo({ url: `/pages/knowledge-detail/index?id=${e.currentTarget.dataset.id}` }) },
  search() { wx.navigateTo({ url: '/pages/search/index' }) },
  showActions(e: any) {
    const id = e.currentTarget.dataset.id
    wx.showActionSheet({ itemList: ['打开资料库', '删除资料库'], success: async ({ tapIndex }) => {
      if (tapIndex === 0) wx.navigateTo({ url: `/pages/knowledge-detail/index?id=${id}` })
      if (tapIndex === 1) {
        const confirm = await new Promise<any>((resolve) => wx.showModal({ title: '删除资料库', content: '资料库中的文档、切片和问答记录都会删除，且无法恢复。', confirmColor: '#c62828', success: resolve }))
        if (!confirm.confirm) return
        try { await deleteKnowledge(id); wx.showToast({ title: '已删除', icon: 'success' }); this.load() } catch (error: any) { wx.showToast({ title: error.message || '删除失败', icon: 'none' }) }
      }
    } })
  },
  create() { wx.navigateTo({ url:'/pages/create/index' }) },
})
