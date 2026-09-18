// 知识库邀请落地页：微信好友点开分享卡片后进入这里。
//
// 这一页只做三件事，不碰任何人的资料：
//  1. 把对方的资料库说清楚（名称 / 简介 / 资料数量），只读不展开内容；
//  2. 好友点「接受」时，把它作为一份只读的共享知识库挂到自己的知识库里；
//  3. 链接失效 / 已被别人领走时，把原因直接写在页面上，不留白屏。
// 一条链接只认第一个接受的好友：接受者可以再转发，但第三个人点开只会看到「已被领取」。
import { acceptKnowledgeShare, getKnowledgeShare } from '../../../services/api'
import { SHARE_HOME_PATH, SHARE_IMAGE } from '../../../utils/share'

type ShareState = 'loading' | 'ready' | 'taken' | 'expired' | 'revoked' | 'missing' | 'error'

/** 每种异常状态的文案：说清「为什么打不开」+「接下来怎么办」。 */
const STATE_COPY: Record<string, { title: string; desc: string }> = {
  taken: { title: '这条邀请已被领取', desc: '一条链接只认第一个接受的好友。可以让分享者重新分享一条给你。' },
  expired: { title: '这条邀请已过期', desc: '邀请链接 7 天内有效，过期后请让好友重新分享一次。' },
  revoked: { title: '分享者已关闭这条邀请', desc: '对方关掉了这条链接，可以请他重新分享。' },
  missing: { title: '这条邀请已失效', desc: '内容可能已被删除，或者链接不完整。可以让好友重新分享一次。' },
  error: { title: '暂时打不开这条邀请', desc: '网络似乎不太稳定，请稍后重试。' },
}

Page({
  data: {
    state: 'loading' as ShareState,
    navHeight: 88,
    token: '',
    name: '',
    description: '',
    documentCount: 0,
    joinedId: '',
    canAccept: false,
    canOpen: false,
    busy: false,
    title: '',
    desc: '',
  },
  onLoad(query: any) {
    this.measureNav()
    const token = String((query && query.kb) || '')
    if (!token) {
      this.setData({ state: 'missing', title: STATE_COPY.missing.title, desc: STATE_COPY.missing.desc })
      return
    }
    this.setData({ token })
    this.load(token)
  },
  // 顶部只留胶囊按钮的安全高度：和问答页同一套算法，避免标题贴到状态栏
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
  load(token: string) {
    this.setData({ state: 'loading' })
    getKnowledgeShare(token).then((view) => {
      if (view.state === 'open' || view.state === 'mine') {
        const joined = String(view.joined_knowledge_id || '')
        this.setData({
          state: 'ready',
          name: view.name || '共享知识库',
          description: view.description || '',
          documentCount: Number(view.document_count || 0),
          joinedId: joined,
          canAccept: !joined,
          canOpen: !!joined,
        })
        return
      }
      const copy = STATE_COPY[view.state] || STATE_COPY.missing
      this.setData({ state: view.state as ShareState, title: copy.title, desc: copy.desc, canAccept: false, canOpen: false })
    }).catch((error: any) => {
      const missing = Number((error && error.statusCode) || 0) === 404
      const copy = missing ? STATE_COPY.missing : STATE_COPY.error
      this.setData({ state: missing ? 'missing' : 'error', title: copy.title, desc: copy.desc })
    })
  },
  retry() {
    if (this.data.token) this.load(this.data.token)
  },
  accept() {
    if (this.data.busy) return
    const token = this.data.token
    if (!token) return
    // 接受是「当前微信用户」的动作：这里隐式走一次微信登录，再挂到自己的共享知识库里
    this.setData({ busy: true })
    acceptKnowledgeShare(token).then((result) => {
      this.setData({ busy: false })
      wx.showToast({ title: result.message || '已加入共享知识库', icon: 'none', duration: 2200 })
      this.openShared(result.knowledge_id)
    }).catch((error: any) => {
      this.setData({ busy: false })
      const status = Number((error && error.statusCode) || 0)
      if (status === 403 || status === 410) {
        // 被别人抢先领走 / 链接失效：刷新一次页面状态，把真实原因写在页面上
        wx.showToast({ title: (error && error.message) || '这条邀请已失效', icon: 'none', duration: 2400 })
        this.load(token)
        return
      }
      wx.showToast({ title: (error && error.message) || '暂时收不进来，稍后再试', icon: 'none', duration: 2400 })
    })
  },
  // 收好后落到「知识库」页，并且默认选中刚收到的这个共享库
  openShared(knowledgeId: string) {
    const id = knowledgeId || this.data.joinedId
    if (!id) { this.enterApp(); return }
    wx.setStorageSync('kb_focus', id)
    wx.switchTab({ url: '/pages/chat/index', fail: () => this.enterApp() })
  },
  enterApp() {
    wx.switchTab({ url: SHARE_HOME_PATH, fail: () => wx.reLaunch({ url: SHARE_HOME_PATH }) })
  },
  // 未接受的好友可以把同一条邀请转给群里：但链接仍然只认第一个接受的人
  onShareAppMessage(): any {
    return {
      title: this.data.name ? `邀请你一起用「${this.data.name}」知识库` : 'cola知识库 · 用 AI 读懂你的资料',
      path: `/package-features/pages/kb-share/index?kb=${this.data.token}`,
      imageUrl: SHARE_IMAGE,
    }
  },
})
