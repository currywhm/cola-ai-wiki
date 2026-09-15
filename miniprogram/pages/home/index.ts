import { getKnowledge, Knowledge, resumeWechatLogin } from '../../services/api'

Page({
  data: { knowledge: [] as Knowledge[], visibleKnowledge: [] as Knowledge[], query: '', searchVisible: false, loadState: 'loading' },
  onShow() { this.load() },
  load() {
    this.setData({ loadState: 'loading' })
    getKnowledge().then((knowledge) => {
      const view = knowledge.map((item) => ({ ...item, displayDate: item.updated_at ? item.updated_at.substring(0, 10) : '刚刚' }))
      this.setData({ knowledge: view, loadState: 'ready' })
      this.applyFilter(view)
    }).catch((error: any) => this.setData({ loadState: error?.loggedOut ? 'loggedout' : 'error', knowledge: [], visibleKnowledge: [] }))
  },
  applyFilter(items: Knowledge[]) { const query = this.data.query.trim().toLowerCase(); this.setData({ visibleKnowledge: items.filter(item => item.name.toLowerCase().includes(query)) }) },
  filterInput(e: any) { this.setData({ query: e.detail.value }); this.applyFilter(this.data.knowledge) },
  toggleSearch() { const next = !this.data.searchVisible; this.setData({ searchVisible: next, query: next ? this.data.query : '' }); if (!next) this.applyFilter(this.data.knowledge) },
  login() { this.setData({ loadState: 'loading' }); resumeWechatLogin().then(() => this.load()).catch(() => this.setData({ loadState: 'error' })) },
  openKnowledge(e: any) { wx.navigateTo({ url: `/pages/knowledge-detail/index?id=${e.currentTarget.dataset.id}` }) },
  createKnowledge() { wx.navigateTo({ url: '/pages/create/index' }) },
})
