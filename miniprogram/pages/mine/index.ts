import { getKnowledge, getMe, logout as apiLogout, MembershipPlan, payPlan, updateMe } from '../../services/api'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'

Page({
  data: {
    user: {} as any,
    loadState: 'loading',
    knowledge: [] as any[],
    stats: { chats: 0, chatPercent: 0, knowledge: 0, knowledgePercent: 0, documents: 0, documentPercent: 0 },
    storageUsedLabel: '0 MB', storageLimitLabel: '300 MB', storagePercent: 0, storageFillStyle: 'width: 0%;',
    planPickerVisible: false,
    selectedTier: 'pro' as 'plus' | 'pro',
    selectedPlan: 'pro_quarterly' as MembershipPlan,
    selectedPlanPrice: '25.00',
    selectedPlanPeriod: '92 天',
    paying: false,
    visiblePlans: [] as any[],
    tips: [
      { title: '先上传，再提问', desc: '文档解析完成后，回答会附带原文出处。', icon: 'spark' },
      { title: '让资料保持清晰', desc: '按项目、主题或时间拆分资料库，查找更快。', icon: 'folder' },
      { title: '用一句话说清问题', desc: '说明范围和目标，能得到更聚焦的回答。', icon: 'dengpao' },
      { title: '让 Cola 帮你整理资料', desc: '上传后自动提取标题、摘要、标签和要点。', icon: 'liebiao' },
      { title: '回答中的出处可以打开', desc: '点击出处即可回到对应文档和页码。', icon: 'file' },
      { title: '选择适合当前任务的模型', desc: '快速模式适合日常问答，深度模式适合复杂问题。', icon: 'atom' },
      { title: '保护你的资料安全', desc: '资料仅用于你的知识库问答，不公开展示。', icon: 'anquanbaozhang' },
    ],
    plans: [
      { id: 'plus_monthly', tier: 'plus', label: '月付', price: '6.90', period: '31 天', note: '按需开通' },
      { id: 'plus_quarterly', tier: 'plus', label: '季付', price: '18.00', period: '92 天', note: '省 13%' },
      { id: 'plus_yearly', tier: 'plus', label: '年付', price: '69.00', period: '365 天', note: '省 17%' },
      { id: 'pro_monthly', tier: 'pro', label: '月付', price: '9.90', period: '31 天', note: '按需开通' },
      { id: 'pro_quarterly', tier: 'pro', label: '季付', price: '25.00', period: '92 天', note: '省 16%' },
      { id: 'pro_yearly', tier: 'pro', label: '年付', price: '99.00', period: '365 天', note: '省 17%' },
    ],
  },
  onShow() { this.load() },
  async syncWechatProfile() {
    try {
      const profile = await new Promise<any>((resolve, reject) => (wx as any).getUserProfile({ desc: '用于显示你的头像和昵称', success: resolve, fail: reject }))
      const userInfo = profile.userInfo || {}
      const user = await updateMe({ nickname: userInfo.nickName || this.data.user.nickname || '微信用户', avatar: '' })
      const normalized = { ...user, displayId: user.id ? user.id.substring(0, 8) : '登录中' }
      getApp<IAppOption>().globalData.user = normalized
      wx.setStorageSync('llmwiki_user', normalized)
      this.setData({ user: normalized }); const nav = this.selectComponent('#pageNav') as any; if (nav) nav.measure()
      wx.showToast({ title: '微信资料已更新', icon: 'success' })
    } catch (error: any) {
      if (!String(error?.errMsg || '').includes('cancel')) wx.showToast({ title: '未完成微信资料授权', icon: 'none' })
    }
  },
  load() { this.setData({loadState:'loading'}); Promise.all([getMe(), getKnowledge()]).then(([user, knowledge]) => { const defaultIndex = knowledge.findIndex((item: any) => item.name === DEFAULT_KNOWLEDGE_NAME); const orderedKnowledge = defaultIndex > 0 ? [knowledge[defaultIndex], ...knowledge.slice(0, defaultIndex), ...knowledge.slice(defaultIndex + 1)] : knowledge; const documents = user.usage?.documents ?? orderedKnowledge.reduce((sum: number, item: any) => sum + (item.document_count || 0), 0); const normalized = { ...user, displayId: user.id ? user.id.substring(0, 8) : '登录中' }; const storageUsed = Number(user.usage?.storage_bytes || 0); const storageLimit = Number(user.limits?.storage_bytes || 300 * 1024 * 1024); const storagePercent = storageLimit ? Math.min(100, Math.round(storageUsed / storageLimit * 100)) : 0; getApp<IAppOption>().globalData.user = normalized; wx.setStorageSync('llmwiki_user', normalized); this.setData({ loadState:'ready', knowledge:orderedKnowledge, user: normalized, storageUsedLabel:this.formatStorage(storageUsed), storageLimitLabel:this.formatStorage(storageLimit), storagePercent, storageFillStyle:`width: ${storagePercent}%;`, stats: { chats: 0, chatPercent: 0, knowledge: user.usage?.knowledge_bases ?? orderedKnowledge.length, knowledgePercent: Math.min(100, orderedKnowledge.length * 20), documents, documentPercent: Math.min(100, documents) } }) }).catch(() => this.setData({loadState:'error',user:{}})) },
  formatStorage(bytes: number) { if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`; return `${Math.max(0, bytes / 1024 / 1024).toFixed(2)} MB` },
  payPro() {
    this.setData({ planPickerVisible: true, visiblePlans: this.data.plans.filter((item: any) => item.tier === this.data.selectedTier) })
  },
  closePlanPicker() { if (!this.data.paying) this.setData({ planPickerVisible: false }) },
  stopPickerBubble() { return },
  chooseTier(e: any) {
    if (this.data.paying) return
    const tier = e.currentTarget.dataset.tier as 'plus' | 'pro'
    const selected = this.data.plans.find((item: any) => item.tier === tier && item.id.endsWith('quarterly')) || this.data.plans.find((item: any) => item.tier === tier)
    if (!selected) return
    this.setData({ selectedTier: tier, selectedPlan: selected.id as MembershipPlan, selectedPlanPrice: selected.price, selectedPlanPeriod: selected.period, visiblePlans: this.data.plans.filter((item: any) => item.tier === tier) })
  },
  choosePlan(e: any) {
    if (this.data.paying) return
    const plan = e.currentTarget.dataset.plan as MembershipPlan
    const selected = this.data.plans.find((item: any) => item.id === plan)
    if (selected) this.setData({ selectedPlan: plan, selectedPlanPrice: selected.price, selectedPlanPeriod: selected.period })
  },
  async confirmPay() {
    if (this.data.paying) return
    this.setData({ paying: true })
    const plan = this.data.selectedPlan as MembershipPlan
    try {
      wx.showLoading({ title: '正在创建订单', mask: true })
      await payPlan(plan)
      wx.hideLoading()
      this.setData({ planPickerVisible: false })
      this.load()
      wx.showToast({ title: '支付成功', icon: 'success' })
    } catch (error: any) {
      wx.hideLoading()
      if (!error.errMsg?.includes('cancel') && !error.cancelled) wx.showToast({ title: error.message || '支付暂不可用', icon: 'none' })
    } finally { this.setData({ paying: false }) }
  },
  showModelInfo() { wx.showModal({ title: '问答模型', content: '当前使用服务端配置的标准模型。管理员可在服务端切换智能模型与备用链路。', showCancel: false }) },
  openLegal(e: any) { wx.navigateTo({ url: `/pages/legal/index?type=${e.currentTarget.dataset.type}` }) },
  logout() { const app = getApp<IAppOption>(); apiLogout(); app.globalData.token = ''; app.globalData.user = null; app.globalData.loggedOut = true; wx.removeStorageSync('llmwiki_token'); wx.removeStorageSync('llmwiki_user'); wx.setStorageSync('llmwiki_logged_out', true); wx.showToast({ title: '已退出登录', icon: 'success' }); setTimeout(() => wx.reLaunch({ url: '/pages/chat/index' }), 500) },
})
