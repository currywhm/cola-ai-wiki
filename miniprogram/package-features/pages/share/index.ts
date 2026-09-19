// 好友分享落地页：微信好友点开分享卡片后进入这里。
// 页面完全公开（不依赖登录态），只读取这一条分享的问答内容与出处。
import { renderMarkdown } from '../../../utils/markdown'
import { decorateSources } from '../../../utils/thread'
import { claimShare, getShare } from '../../../services/api'
import { decorateShareFiles, openShareFile, saveShareFile, ShareFileItem } from '../../utils/share-file'
import { SHARE_HOME_PATH, SHARE_IMAGE, shareTitle } from '../../../utils/share'

type ShareState = 'loading' | 'ready' | 'missing' | 'error'

/** 主按钮下面那行说明：先讲清楚点下去会发生什么，再让人点。 */
function openHintOf(hasText: boolean, fileCount: number): string {
  if (fileCount && hasText) return `对话与 ${fileCount} 个文件会收进你的「共享知识库」`
  if (fileCount) return fileCount === 1 ? '文件会收进你「共享知识库」的「分享的文件」里' : `${fileCount} 个文件会收进你「共享知识库」的「分享的文件」里`
  return '这篇对话会存进你的「共享知识库」，之后可以接着追问'
}

Page({
  data: {
    state: 'loading' as ShareState,
    navHeight: 88,
    shareId: '',
    title: '',
    question: '',
    html: '',
    sources: [] as any[],
    files: [] as ShareFileItem[],
    openHint: '',
    claimBusy: false,
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
        files: decorateShareFiles(card && card.files),
        openHint: openHintOf(!!String(answer || '').trim(), ((card && card.files) || []).length),
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
  // 找这一行对应的文件：data-index 是后端给的取件序号，不是数组下标
  findFile(e: any): ShareFileItem | null {
    const index = Number((e.currentTarget.dataset || {}).index)
    const files = this.data.files || []
    for (let cursor = 0; cursor < files.length; cursor += 1) {
      if (Number(files[cursor].index) === index) return files[cursor]
    }
    return null
  },
  // 点一行预览，右侧按钮保存/转发到聊天
  openFile(e: any) {
    const item = this.findFile(e)
    if (item) openShareFile(this.data.shareId, item)
  },
  saveFile(e: any) {
    const item = this.findFile(e)
    if (item) saveShareFile(this.data.shareId, item)
  },
  openApp() {
    if (this.data.claimBusy) return
    const id = this.data.shareId
    if (!id) { this.enterApp(); return }
    // 收件是当前微信用户自己的动作：这里隐式走一次微信登录，再把它收进自己的「共享知识库」
    this.setData({ claimBusy: true })
    // 收件要复制文件并抽取正文，可能几十秒：转圈上带服务端进度，别让好友以为卡死
    wx.showLoading({ title: '正在收取', mask: true })
    claimShare(id, (label) => wx.showLoading({ title: String(label || '正在收取').slice(0, 12), mask: true })).then((result) => {
      wx.hideLoading()
      this.setData({ claimBusy: false })
      const knowledgeId = String((result && result.knowledge_id) || '')
      wx.showToast({ title: (result && result.message) || '已收进共享知识库', icon: 'none', duration: 2200 })
      setTimeout(() => this.openLibrary(knowledgeId), 900)
    }).catch((error: any) => {
      wx.hideLoading()
      this.setData({ claimBusy: false })
      // 登录没走通 / 网络异常 / 空间不足：把原因说清楚再退回首页，不让好友卡在这一页
      wx.showToast({ title: (error && error.message) || '暂时收不进来，稍后再试', icon: 'none', duration: 2600 })
      setTimeout(() => this.enterApp(), 1000)
    })
  },
  // 收好后直接落到他自己的共享知识库：文件在「分享的文件」里，对话存成一篇可追问的资料
  openLibrary(knowledgeId: string) {
    if (!knowledgeId) { this.enterApp(); return }
    wx.navigateTo({ url: `/package-features/pages/knowledge-detail/index?id=${encodeURIComponent(knowledgeId)}`, fail: () => this.enterApp() })
  },
  enterApp() {
    wx.switchTab({ url: SHARE_HOME_PATH, fail: () => wx.reLaunch({ url: SHARE_HOME_PATH }) })
  },
  // 分享页也能再转发一次：好友再点开看到的还是这条回答
  onShareAppMessage(): any {
    return { title: this.data.title || 'cola知识库 · 用 AI 读懂你的资料', path: `/package-features/pages/share/index?id=${this.data.shareId}`, imageUrl: SHARE_IMAGE }
  },
})
