// 问AI页：一个吉祥物 + 一句标题 + 若干入口胶囊 + 一个输入框，其余全部留白
// 登录前展示导入引导（图1），登录后展示能力入口与推荐提问（图2）
import { deleteConversation, getConversations, goLogin, isLoggedIn, pinConversation } from '../../services/api'

// 推荐提问池：每次「换一换」向后轮换，避免永远展示同一组问题
const SUGGESTION_POOL: string[] = [
  '这个知识库主要讲了什么？',
  '帮我总结资料里的核心要点',
  '把长文整理成一份知识图解',
  '这些资料之间有什么关联？',
  '基于资料写一份调研提纲',
  '哪些内容值得我优先阅读？',
  '帮我梳理时间线和关键事件',
  '用通俗的话解释里面的专业概念',
]

const BOOT_SPLASH_MIN_MS = 1100

// 启动页归属小程序冷启动：标记挂在 app.globalData 上，切换 tab 不会重新播放
function splashShown(): boolean {
  try { return !!getApp<IAppOption>().globalData.splashShown } catch (e) { return true }
}
function markSplashShown() {
  try { getApp<IAppOption>().globalData.splashShown = true } catch (e) { /* 取不到 app 时忽略 */ }
}

let bootStartedAt = 0

function pickSuggestions(start: number): string[] {
  const total = SUGGESTION_POOL.length
  return [0, 1, 2].map((offset) => SUGGESTION_POOL[(start + offset) % total])
}

// 微信登录在 app.onLaunch 中异步完成，这里同时看内存态与本地缓存
function readLoggedIn(): boolean {
  return isLoggedIn()
}

Page({
  data: {
    loggedIn: false,
    booting: true,
    suggestionStart: 0,
    // 历史对话抽屉：数据全部来自后端 /api/conversations，本页不缓存会话内容
    historyVisible: false,
    historyLoading: false,
    historyItems: [] as any[],
    suggestions: pickSuggestions(0),
  },
  onLoad() {
    if (splashShown()) { this.setData({ booting: false }); return }
    bootStartedAt = Date.now()
    this.setData({ booting: true })
  },
  onShow() {
    this.syncTabBar()
    this.syncLogin()
    this.finishBoot()
  },
  onHide() { this.finishBoot() },
  syncLogin() {
    const loggedIn = readLoggedIn()
    if (loggedIn !== this.data.loggedIn) this.setData({ loggedIn })
  },
  requireLogin(): boolean {
    if (isLoggedIn()) return true
    goLogin()
    return false
  },
  finishBoot() {
    if (!this.data.booting) return
    const elapsed = Date.now() - bootStartedAt
    const delay = Math.max(0, BOOT_SPLASH_MIN_MS - elapsed)
    setTimeout(() => { markSplashShown(); this.setData({ booting: false }) }, delay)
  },
  // 三个入口都落到真实功能：建库走创建页，其余把诉求带进问答页
  onAction(e: any) {
    if (!this.requireLogin()) return
    const action = String(e.currentTarget.dataset.action || '')
    if (action === 'build') {
      wx.navigateTo({ url: '/package-features/pages/create/index?from=library' })
      return
    }
    const drafts: Record<string, string> = {
      diagram: '把当前知识库里的长文整理成一份知识图解，按主题分组并列出要点。',
      report: '基于当前知识库的资料，帮我整理一份调研报告，包含背景、结论和关键数据。',
    }
    this.openChat(drafts[action] || '', false)
  },
  refreshActions() {
    const start = (this.data.suggestionStart + 3) % SUGGESTION_POOL.length
    this.setData({ suggestionStart: start, suggestions: pickSuggestions(start) })
  },
  onSuggestion(e: any) {
    if (!this.requireLogin()) return
    const text = String(e.currentTarget.dataset.text || '')
    if (!text) return
    this.openChat(text, true)
  },
  // 未登录时的「在线提问」：同样进入问答页，登录由 app 启动流程自动完成
  onPromoAsk() { this.openChat('', false) },
  // 进入问答：独立页面（不带底部 bar），把提问内容一起带过去
  openChat(draft: string, autoSend: boolean) {
    if (!this.requireLogin()) return
    if (draft) wx.setStorageSync('qa_draft', draft)
    wx.setStorageSync('qa_auto_send', !!autoSend)
    wx.setStorageSync('qa_mode', 'knowledge')
    wx.navigateTo({ url: '/package-features/pages/qa/index' })
  },
  // 顶部栏左侧的历史对话：与对话页共用同一个抽屉，取当前用户的全部会话
  openHistory() {
    if (!this.requireLogin()) return
    if (this.data.historyVisible) return
    this.setData({ historyVisible: true, historyLoading: true })
    getConversations({ knowledgeId: '', folderId: '' }).then((items) => {
      this.setData({ historyItems: items || [], historyLoading: false })
    }).catch(() => {
      this.setData({ historyVisible: false, historyLoading: false, historyItems: [] })
      wx.showToast({ title: '历史对话加载失败，请稍后重试', icon: 'none' })
    })
  },
  closeHistory() { this.setData({ historyVisible: false }) },
  // 选中一条历史：进问答页并直接恢复那条会话（会话与所属知识库随本地缓存带过去）
  pickHistory(e: any) {
    if (!this.requireLogin()) return
    const id = String((e.detail && e.detail.id) || '')
    if (!id) return
    const item = (this.data.historyItems as any[]).find((row: any) => row.id === id) || {}
    this.setData({ historyVisible: false })
    wx.setStorageSync('qa_conversation_id', id)
    if (item.knowledge_id) wx.setStorageSync('qa_knowledge_id', String(item.knowledge_id))
    else wx.removeStorageSync('qa_knowledge_id')
    wx.removeStorageSync('qa_draft')
    wx.removeStorageSync('qa_auto_send')
    wx.navigateTo({ url: '/package-features/pages/qa/index' })
  },
  // 新建对话：清掉待恢复的会话，直接进一个空白问答页
  newConversation() {
    if (!this.requireLogin()) return
    this.setData({ historyVisible: false })
    wx.removeStorageSync('qa_conversation_id')
    wx.removeStorageSync('qa_knowledge_id')
    this.openChat('', false)
  },
  // 置顶 / 取消置顶：只改当前用户自己的会话，置顶后排到列表最前
  pinHistory(e: any) {
    if (!this.requireLogin()) return
    const id = String((e.detail && e.detail.id) || '')
    const pinned = !!(e.detail && e.detail.pinned)
    if (!id) return
    pinConversation(id, pinned).then(() => {
      this.setData({ historyItems: this.data.historyItems.map((item: any) => item.id === id ? { ...item, pinned: pinned ? 1 : 0 } : item) })
      wx.showToast({ title: pinned ? '已置顶' : '已取消置顶', icon: 'none' })
    }).catch(() => wx.showToast({ title: '操作失败，请稍后重试', icon: 'none' }))
  },
  // 删除：二次确认后再删，删完立刻从列表里移除，不走重新拉取
  removeHistory(e: any) {
    if (!this.requireLogin()) return
    const id = String((e.detail && e.detail.id) || '')
    if (!id) return
    wx.showModal({
      title: '删除这条对话',
      content: '删除后这条对话的内容不再保留，无法恢复。',
      confirmText: '删除',
      confirmColor: '#d92d20',
      success: (res) => {
        if (!res.confirm) return
        deleteConversation(id).then(() => {
          this.setData({ historyItems: this.data.historyItems.filter((item: any) => item.id !== id) })
          wx.showToast({ title: '已删除', icon: 'success' })
        }).catch(() => wx.showToast({ title: '删除失败，请稍后重试', icon: 'none' }))
      },
    })
  },
  // 底部输入框是入口按钮：点击后进入问答页
  onComposerTap() { this.openChat('', false) },
  // 自定义 tabBar 选中态：四个 tab 首页才显示底部导航
  syncTabBar() {
    if (typeof this.getTabBar !== 'function') return
    const bar = this.getTabBar() as any
    if (bar && typeof bar.setData === 'function') bar.setData({ selected: 0, hidden: false })
  },
})
