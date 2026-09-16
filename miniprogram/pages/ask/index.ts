// 问AI页：一个吉祥物 + 一句标题 + 若干入口胶囊 + 一个输入框，其余全部留白
// 登录前展示导入引导（图1），登录后展示能力入口与推荐提问（图2）

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
  try {
    const app = getApp<IAppOption>()
    if (app && app.globalData && (app.globalData.token || app.globalData.user)) return true
  } catch (e) { /* 忽略 */ }
  return !!wx.getStorageSync('llmwiki_token') || !!wx.getStorageSync('llmwiki_user')
}

Page({
  data: {
    navHeight: 88,
    loggedIn: false,
    booting: true,
    suggestionStart: 0,
    suggestions: pickSuggestions(0),
  },
  onLoad() {
    this.measureNav()
    if (splashShown()) { this.setData({ booting: false }); return }
    bootStartedAt = Date.now()
    this.setData({ booting: true })
  },
  onShow() {
    this.syncTabBar()
    this.syncLogin()
    this.finishBoot()
    // 冷启动时登录结果通常晚于首帧返回，补一次同步，保证「登录前 / 登录后」两态及时切换
    setTimeout(() => this.syncLogin(), 900)
  },
  onHide() { this.finishBoot() },
  // 顶部不留标题栏，但必须避让胶囊按钮，高度随机型变化
  measureNav() {
    try {
      const api = wx as any
      const info = api.getWindowInfo ? api.getWindowInfo() : wx.getSystemInfoSync()
      const rect = wx.getMenuButtonBoundingClientRect()
      const status = info.statusBarHeight || 20
      const valid = rect && rect.height > 0 && rect.top >= status
      const height = valid ? Math.max(44, (rect.top - status) * 2 + rect.height) : 44
      this.setData({ navHeight: status + height })
    } catch (e) { this.setData({ navHeight: 88 }) }
  },
  syncLogin() {
    const loggedIn = readLoggedIn()
    if (loggedIn !== this.data.loggedIn) this.setData({ loggedIn })
  },
  finishBoot() {
    if (!this.data.booting) return
    const elapsed = Date.now() - bootStartedAt
    const delay = Math.max(0, BOOT_SPLASH_MIN_MS - elapsed)
    setTimeout(() => { markSplashShown(); this.setData({ booting: false }) }, delay)
  },
  // 三个入口都落到真实功能：建库走创建页，其余把诉求带进问答页
  onAction(e: any) {
    const action = String(e.currentTarget.dataset.action || '')
    if (action === 'build') {
      wx.navigateTo({ url: '/pages/create/index' })
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
    const text = String(e.currentTarget.dataset.text || '')
    if (!text) return
    this.openChat(text, true)
  },
  // 未登录时的「在线提问」：同样进入问答页，登录由 app 启动流程自动完成
  onPromoAsk() { this.openChat('', false) },
  // 进入问答：独立页面（不带底部 bar），把提问内容一起带过去
  openChat(draft: string, autoSend: boolean) {
    if (draft) wx.setStorageSync('qa_draft', draft)
    wx.setStorageSync('qa_auto_send', !!autoSend)
    wx.setStorageSync('qa_mode', 'knowledge')
    wx.navigateTo({ url: '/pages/qa/index' })
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
