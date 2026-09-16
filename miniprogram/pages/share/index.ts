// 好友分享落地页：微信好友点开分享卡片后进入这里。
// 页面完全公开（不依赖登录态），只读取这一条分享的问答内容与出处。
import { renderMarkdown } from '../../utils/markdown'
import { decorateSources } from '../../utils/thread'
import { getShare } from '../../services/api'
import { SHARE_HOME_PATH, SHARE_IMAGE, shareTitle } from '../../utils/share'

type ShareState = 'loading' | 'ready' | 'missing' | 'error'

Page({
  data: {
    state: 'loading' as ShareState,
    navHeight: 88,
    shareId: '',
    title: '',
    question: '',
    html: '',
    sources: [] as any[],
  },
  onLoad(query: any) {
    this.measureNav()
    const id = String((query && query.id) || '')
    if (!id) {
      this.setData({ state: 'missing' })
      return
    }
    this.setData({ shareId: id })
    this.load(id)
  },
  // 顶部只留胶囊按钮的安全高度：与问答页同一套算法，避免标题贴到状态栏
  measureNav() {
    try {
      const api = wx as any
      const info = api.getWindowInfo ? api.getWindowInfo() : wx.getSystemInfoSync()
      const rect = wx.getMenuButtonBoundingClientRect()
      const status = info.statusBarHeight || 20
      const valid = rect && rect.height > 0 && rect.top >= status
      const height = valid ? Math.max(44, (rect.top - status) * 2 + rect.height) : 44
      this.setData({ navHeight: status + height })
    } catch (e) {
      this.setData({ navHeight: 88 })
    }
  },
  load(id: string) {
    this.setData({ state: 'loading' })
    getShare(id).then((card) => {
      const question = String((card && card.question) || '')
      const answer = String((card && card.answer) || '')
      this.setData({
        state: 'ready',
        question,
        html: renderMarkdown(answer),
        title: shareTitle(question, answer),
        sources: decorateSources(card && card.sources).map((source: any) => ({
          key: `${source.filename}-${source.page_number}-${source.url}`,
          filename: source.filename || '公开网页',
          iconName: source.iconName || 'recent-text',
          meta: source.url ? '公开网页' : (source.page_number ? `第 ${source.page_number} 页` : '知识库资料'),
        })),
      })
    }).catch((error: any) => {
      this.setData({ state: error && error.statusCode === 404 ? 'missing' : 'error' })
    })
  },
  retry() {
    if (this.data.shareId) this.load(this.data.shareId)
  },
  openApp() {
    wx.switchTab({ url: SHARE_HOME_PATH, fail: () => wx.reLaunch({ url: SHARE_HOME_PATH }) })
  },
  // 分享页也能再转发一次：好友再点开看到的还是这条回答
  onShareAppMessage(): any {
    return { title: this.data.title || 'cola知识库 · 用 AI 读懂你的资料', path: `/pages/share/index?id=${this.data.shareId}`, imageUrl: SHARE_IMAGE }
  },
})
