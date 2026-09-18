import { clearAuthSession, getKnowledge, getMe, goLogin, isLoggedIn, logout as apiLogout, updateMe, uploadAvatar, userAvatarUrl } from '../../services/api'
import { localizeImage } from '../../services/media'
import { openPrivacyContract, readPrivacySetting, requestPrivacyAuthorize, warnPrivacyRequired } from '../../services/privacy'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'

Page({
  data: {
    loggedIn: false,
    user: {} as any,
    loadState: 'loading',
    knowledge: [] as any[],
    stats: { chats: 0, chatPercent: 0, knowledge: 0, knowledgePercent: 0, documents: 0, documentPercent: 0 },
    storageUsedLabel: '0 MB', storageLimitLabel: '300 MB', storagePercent: 0, storageFillStyle: 'width: 0%;',
    statusLine: '正在连接微信账号…', storageNote: '试用赠送 300MB 空间', quotaLabel: '本月积分 0 / 600',
    trialActive: true, memberActive: false,
    logoutVisible: false,
    loggingOut: false,
    // 账号设置：头像走微信官方「头像选择」（<button open-type="chooseAvatar">），
    // 昵称走官方「昵称填写」（<input type="nickname">，键盘上方一键选微信昵称）。
    // 平台不提供静默读取头像昵称的接口（wx.getUserProfile / open-data 都已回收），
    // 所以设置层分两步：先官方隐私同意，再用官方「头像昵称填写」一键选用。
    // privacyNeeded=true 时，设置层只显示一个官方「同意」按钮。
    privacyNeeded: false,
    privacyContractName: '',
    accountVisible: false,
    nicknameDraft: '',
    nicknameFocus: false,
    avatarPreview: '',
    avatarSrc: '',
    savingProfile: false,
  },
  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) {
      this.getTabBar().setData({ selected: 3, hidden: false })
    }
    if (!isLoggedIn()) {
      this.setData({ loggedIn: false, loadState: 'guest', user: {}, knowledge: [] })
      return
    }
    this.setData({ loggedIn: true })
    this.load()
  },
  // 账号设置不能再走 wx.getUserProfile / wx.getUserInfo：两个接口都已被微信回收，
  // 而且当前小程序调 wx.getUserInfo 直接报 errno 112（隐私协议里没声明该 scope），
  // <open-data> 也只会展示「微信用户」+ 灰色头像。
  // 平台留下的唯一合规通道是「头像昵称填写能力」：
  //   头像用 <button open-type="chooseAvatar">，昵称用 <input type="nickname">（键盘上方一键选微信昵称）。
  // 所以设置层的顺序是：官方隐私同意（未同意时）→ 点一下头像 → 点一下昵称 → 保存。
  openAccountSettings() {
    if (!this.requireLogin()) return
    // 标题栏是原生组件，不收起来会盖住弹层底部的「保存」按钮
    this.setTabBarHidden(true)
    this.setData({
      accountVisible: true,
      nicknameDraft: (this.data.user && this.data.user.nickname) || '',
      avatarPreview: '',
      nicknameFocus: false,
    })
    // 微信侧还没记录同意时，弹层第一步只给官方「同意」按钮（open-type="agreePrivacyAuthorization"）
    readPrivacySetting().then((setting) => {
      this.setData({
        privacyNeeded: !!setting.needAuthorization,
        privacyContractName: setting.privacyContractName || '《cola知识库小程序隐私保护指引》',
      })
    }).catch(() => { /* 查不到就当成已同意：后面每个官方控件自己会再判断一次 */ })
  },
  // 官方「同意」按钮被点：open-type="agreePrivacyAuthorization" 会自己拉起微信官方隐私弹窗。
  // 这里只做一个 1.2 秒兜底——万一当前基础库没弹（或用户早就同意过），
  // 主动调一次 wx.requirePrivacyAuthorize，保证「点一下一定有反应」，弹的仍然是官方弹窗。
  onAgreeTap() {
    const self = this as any
    if (self.agreeTimer) clearTimeout(self.agreeTimer)
    self.agreeTimer = setTimeout(() => {
      if (!this.data.accountVisible || !this.data.privacyNeeded) return
      requestPrivacyAuthorize().then((granted) => {
        if (granted) this.enterAccountPick()
        else warnPrivacyRequired('设置微信头像昵称')
      })
    }, 1200)
  },
  // 用户在官方弹窗里点了「同意」：直接进入选用微信头像昵称这一步
  onPrivacyAgreed() {
    const self = this as any
    if (self.agreeTimer) { clearTimeout(self.agreeTimer); self.agreeTimer = null }
    this.enterAccountPick()
  },
  // 同意之后进入「选用微信头像昵称」，并把同意状态同步给全局（导入资料那边要用）
  enterAccountPick() {
    getApp<IAppOption>().globalData.privacyGranted = true
    this.setData({ privacyNeeded: false })
  },
  // 「查看《小程序用户隐私保护指引》全文」：优先打开微信官方页面，读到的就是刚才同意的那一份
  openPrivacyGuide() {
    if (openPrivacyContract()) return
    wx.navigateTo({ url: '/package-features/pages/legal/index?type=guide' })
  },
  closeAccountSettings() {
    if (this.data.savingProfile) return
    const self = this as any
    if (self.agreeTimer) { clearTimeout(self.agreeTimer); self.agreeTimer = null }
    this.setTabBarHidden(false)
    this.setData({ accountVisible: false, avatarPreview: '', nicknameFocus: false })
  },
  onNicknameInput(e: any) { this.setData({ nicknameDraft: e.detail.value }) },
  // 昵称整行都能点：点一下聚焦 <input type="nickname">，键盘上方就会给出「微信昵称」，点一下即选用
  focusNickname() { this.setData({ nicknameFocus: true }) },
  onNicknameBlur() { this.setData({ nicknameFocus: false }) },
  // 微信官方头像选择：回调给的是本机临时路径（http://tmp/... 或 wxfile://...），
  // 只在本次会话有效，所以保存时要立刻上传到服务端。
  onChooseAvatar(e: any) {
    const path = String((e && e.detail && e.detail.avatarUrl) || '')
    if (!path) return
    this.setData({ avatarPreview: path })
  },
  // 后端存的是相对地址，补上 base 再本地化——微信渲染层不接受 http:// 图片
  syncAvatar() {
    // 先看页面数据；load() 里 globalData 比 setData 先就绪，所以再拿 globalData 兜一次
    const globalUser: any = getApp<IAppOption>().globalData.user
    const source = (this.data.user && this.data.user.avatar) || (globalUser && globalUser.avatar) || ''
    const raw = userAvatarUrl(source)
    if (!raw) { this.setData({ avatarSrc: '' }); return }
    localizeImage(raw).then((path) => this.setData({ avatarSrc: path || '' })).catch(() => this.setData({ avatarSrc: '' }))
  },
  async saveProfile() {
    const nickname = String(this.data.nicknameDraft || '').trim()
    const preview = String(this.data.avatarPreview || '')
    if (!nickname && !preview) { wx.showToast({ title: '先点一下头像，选用微信头像', icon: 'none' }); return }
    this.setData({ savingProfile: true })
    let loading = false
    try {
      const payload: { nickname?: string; avatar?: string } = {}
      // 昵称和头像各自独立：只改其中一个时不动另一个（后端未传字段保持原值）
      if (nickname) payload.nickname = nickname
      if (preview) {
        // 已经是服务端地址就直接用；微信给的临时路径要先上传换取可长期使用的地址
        if (/^https?:\/\//i.test(preview) || preview.indexOf('/api/') === 0) payload.avatar = preview
        else { wx.showLoading({ title: '正在保存头像', mask: true }); loading = true; payload.avatar = (await uploadAvatar(preview)).avatar || '' }
      }
      const saved = await updateMe(payload)
      if (loading) { wx.hideLoading(); loading = false }
      // 后端 PATCH 只回 id / nickname / avatar，合并回完整用户对象，不把会员额度等字段弄丢
      const merged = { ...(this.data.user || {}), ...saved, displayId: String(saved.id || (this.data.user && this.data.user.id) || '').substring(0, 8) }
      getApp<IAppOption>().globalData.user = merged
      wx.setStorageSync('llmwiki_user', merged)
      this.setData({ user: merged, accountVisible: false, avatarPreview: '' })
      this.syncAvatar()
      this.setTabBarHidden(false)
      const nav = this.selectComponent('#pageNav') as any; if (nav) nav.measure()
      wx.showToast({ title: '已保存', icon: 'success' })
    } catch (error: any) {
      wx.showToast({ title: error?.message || '保存失败，请重试', icon: 'none' })
    } finally {
      if (loading) wx.hideLoading()
      this.setData({ savingProfile: false })
    }
  },
  // 不用数组解构赋值：开发者工具 babel 会为解构注入 slicedToArray helper，注入失败会让整页模块加载中断（白屏）
  load() { this.setData({loadState:'loading'}); Promise.all([getMe(), getKnowledge()]).then((pair) => { const user = pair[0]; const knowledge = pair[1]; const defaultIndex = knowledge.findIndex((item: any) => item.name === DEFAULT_KNOWLEDGE_NAME); const orderedKnowledge = defaultIndex > 0 ? [knowledge[defaultIndex], ...knowledge.slice(0, defaultIndex), ...knowledge.slice(defaultIndex + 1)] : knowledge; const documents = user.usage?.documents ?? orderedKnowledge.reduce((sum: number, item: any) => sum + (item.document_count || 0), 0); const normalized = { ...user, displayId: user.id ? user.id.substring(0, 8) : '登录中' }; const storageUsed = Number(user.usage?.storage_bytes || 0); const storageLimit = Number(user.limits?.storage_bytes || 300 * 1024 * 1024); const storagePercent = storageLimit ? Math.min(100, Math.round(storageUsed / storageLimit * 100)) : 0; const trial = user.trial || { active: false, days_left: 0 }; const period = user.period || { days_left: 0 }; const quota = user.quota || { credits_limit: 600, credits_used: 0, credits_left: 0 }; const member = Boolean(user.entitlements && user.entitlements.member); const statusLine = member ? `${user.membership_label || '会员'} · 租期剩余 ${period.days_left} 天` : (trial.active ? `免费试用剩余 ${trial.days_left} 天` : '免费试用已结束 · 资料仍可查看'); const storageNote = member ? `${user.membership_label || '会员'} ${this.formatStorage(storageLimit)} 空间` : (trial.active ? '试用赠送 300MB 空间' : '试用已结束 · 资料保留可查看'); const quotaLabel = `本月积分 ${quota.credits_used} / ${quota.credits_limit}`; getApp<IAppOption>().globalData.user = normalized; wx.setStorageSync('llmwiki_user', normalized); this.syncAvatar(); this.setData({ loadState:'ready', knowledge:orderedKnowledge, user: normalized, statusLine, storageNote, quotaLabel, trialActive: trial.active, memberActive: member, storageUsedLabel:this.formatStorage(storageUsed), storageLimitLabel:this.formatStorage(storageLimit), storagePercent, storageFillStyle:`width: ${storagePercent}%;`, stats: { chats: 0, chatPercent: 0, knowledge: user.usage?.knowledge_bases ?? orderedKnowledge.length, knowledgePercent: Math.min(100, orderedKnowledge.length * 20), documents, documentPercent: Math.min(100, documents) } }) }).catch(() => this.setData({loadState:'error',user:{}})) },
  formatStorage(bytes: number) { if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`; return `${Math.max(0, bytes / 1024 / 1024).toFixed(2)} MB` },
  // 未登录页不发起个人资料请求；所有真实功能入口再由这里统一拦截。
  requireLogin(): boolean {
    if (isLoggedIn()) return true
    goLogin()
    return false
  },
  // 账号设置和退出确认會收起原生 tabBar，避免遮住底部按钮。
  setTabBarHidden(hidden: boolean) {
    if (typeof this.getTabBar !== 'function') return
    const bar = this.getTabBar()
    if (bar && typeof bar.setData === 'function') bar.setData({ hidden })
  },
  openUsage() { if (!this.requireLogin()) return; wx.navigateTo({ url: '/package-features/pages/usage/index' }) },
  openMembership() { if (!this.requireLogin()) return; wx.navigateTo({ url: '/package-features/pages/membership/index' }) },
  openTips() { if (!this.requireLogin()) return; wx.navigateTo({ url: '/package-features/pages/tips/index' }) },
  openLogin() { goLogin() },
  stopPickerBubble() { return },
  openLegal(e: any) { wx.navigateTo({ url: `/package-features/pages/legal/index?type=${e.currentTarget.dataset.type}` }) },
  // 联系客服：会话窗口由微信原生的 open-type="contact" 拉起，前端不渲染聊天内容、
  // 也不保存聊天记录（客服消息走后端 /api/wechat/kf/callback 落库）。
  // 这里只记一条本地埋点，并在用户从会话卡片带参数回来时按需跳页。
  onContact(e: any) {
    const detail = (e && e.detail) || {}
    try {
      wx.setStorageSync('cola_kf_entry', { from: 'mine', at: Date.now() })
    } catch { /* 埋点写不进去不影响使用 */ }
    const path = String(detail.path || '')
    const query = String(detail.query || '')
    // 卡片指回「我的」本身就是当前页，不再入栈
    if (!path || path.indexOf('/pages/mine/index') === 0) return
    wx.navigateTo({ url: `/${path.replace(/^\//, '')}${query ? `?${query}` : ''}`, fail: () => {} })
  },
  openLogout() {
    if (!this.requireLogin()) return
    if (this.data.loggingOut) return
    this.setTabBarHidden(true)
    this.setData({ logoutVisible: true })
  },
  closeLogout() {
    if (this.data.loggingOut) return
    this.setTabBarHidden(false)
    this.setData({ logoutVisible: false })
  },
  stopLogoutBubble() { return },
  async confirmLogout() {
    if (this.data.loggingOut) return
    this.setData({ loggingOut: true })
    try {
      await apiLogout()
    } finally {
      clearAuthSession()
      this.setTabBarHidden(false)
      this.setData({ logoutVisible: false, loggingOut: false })
      wx.reLaunch({ url: '/pages/login/index' })
    }
  },
})
