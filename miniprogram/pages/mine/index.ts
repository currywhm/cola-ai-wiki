import { contentAssetUrl, getKnowledge, getMe, getPayPlans, getTipsIndex, logout as apiLogout, MembershipPlan, payPlan, updateMe } from '../../services/api'
import { applyLocalized, localizeImages } from '../../services/media'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'

Page({
  data: {
    user: {} as any,
    loadState: 'loading',
    knowledge: [] as any[],
    stats: { chats: 0, chatPercent: 0, knowledge: 0, knowledgePercent: 0, documents: 0, documentPercent: 0 },
    storageUsedLabel: '0 MB', storageLimitLabel: '300 MB', storagePercent: 0, storageFillStyle: 'width: 0%;',
    statusLine: '正在连接微信账号…', storageNote: '试用赠送 300MB 空间', quotaLabel: '本月问答 0 / 200 次',
    trialActive: true, memberActive: false,
    tierDesc: { plus: '10 个资料库 · 10GB', pro: '50 个资料库 · 30GB' },
    tierBenefit: { plus: '每月 1,000 次问答 · 单文件 100MB', pro: '每月 5,000 次问答 · 单文件 300MB' },
    activeBenefit: '每月 1,000 次问答 · 单文件 100MB', membershipCopy: 'Plus ¥12/月 · Pro ¥29/月',
    planPickerVisible: false,
    selectedTier: 'pro' as 'plus' | 'pro',
    selectedPlan: 'pro_quarterly' as MembershipPlan,
    selectedPlanPrice: '75.00',
    selectedPlanPeriod: '92 天',
    paying: false,
    visiblePlans: [] as any[],
    tipGroups: [] as any[],
    tipsLoading: true,
    tipsFailed: false,
    plans: [
      { id: 'plus_monthly', tier: 'plus', label: '月付', price: '12.00', period: '31 天', note: '按需开通' },
      { id: 'plus_quarterly', tier: 'plus', label: '季付', price: '30.00', period: '92 天', note: '省 17%' },
      { id: 'plus_yearly', tier: 'plus', label: '年付', price: '108.00', period: '365 天', note: '省 25%' },
      { id: 'pro_monthly', tier: 'pro', label: '月付', price: '29.00', period: '31 天', note: '按需开通' },
      { id: 'pro_quarterly', tier: 'pro', label: '季付', price: '75.00', period: '92 天', note: '省 14%' },
      { id: 'pro_yearly', tier: 'pro', label: '年付', price: '258.00', period: '365 天', note: '省 26%' },
    ],
  },
  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) {
      this.getTabBar().setData({ selected: 3, hidden: false })
    }
    this.load()
    this.loadTips()
    this.loadPlans()
  },
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
  // 不用数组解构赋值：开发者工具 babel 会为解构注入 slicedToArray helper，注入失败会让整页模块加载中断（白屏）
  load() { this.setData({loadState:'loading'}); Promise.all([getMe(), getKnowledge()]).then((pair) => { const user = pair[0]; const knowledge = pair[1]; const defaultIndex = knowledge.findIndex((item: any) => item.name === DEFAULT_KNOWLEDGE_NAME); const orderedKnowledge = defaultIndex > 0 ? [knowledge[defaultIndex], ...knowledge.slice(0, defaultIndex), ...knowledge.slice(defaultIndex + 1)] : knowledge; const documents = user.usage?.documents ?? orderedKnowledge.reduce((sum: number, item: any) => sum + (item.document_count || 0), 0); const normalized = { ...user, displayId: user.id ? user.id.substring(0, 8) : '登录中' }; const storageUsed = Number(user.usage?.storage_bytes || 0); const storageLimit = Number(user.limits?.storage_bytes || 300 * 1024 * 1024); const storagePercent = storageLimit ? Math.min(100, Math.round(storageUsed / storageLimit * 100)) : 0; const trial = user.trial || { active: false, days_left: 0 }; const period = user.period || { days_left: 0 }; const quota = user.quota || { questions_limit: 200, questions_used: 0, questions_left: 0 }; const member = Boolean(user.entitlements && user.entitlements.member); const statusLine = member ? `${user.membership_label || '会员'} · 租期剩余 ${period.days_left} 天` : (trial.active ? `免费试用剩余 ${trial.days_left} 天` : '免费试用已结束 · 资料仍可查看'); const storageNote = member ? `${user.membership_label || '会员'} ${this.formatStorage(storageLimit)} 空间` : (trial.active ? '试用赠送 300MB 空间' : '试用已结束 · 资料保留可查看'); const quotaLabel = `本月问答 ${quota.questions_used} / ${quota.questions_limit} 次`; getApp<IAppOption>().globalData.user = normalized; wx.setStorageSync('llmwiki_user', normalized); this.setData({ loadState:'ready', knowledge:orderedKnowledge, user: normalized, statusLine, storageNote, quotaLabel, trialActive: trial.active, memberActive: member, storageUsedLabel:this.formatStorage(storageUsed), storageLimitLabel:this.formatStorage(storageLimit), storagePercent, storageFillStyle:`width: ${storagePercent}%;`, stats: { chats: 0, chatPercent: 0, knowledge: user.usage?.knowledge_bases ?? orderedKnowledge.length, knowledgePercent: Math.min(100, orderedKnowledge.length * 20), documents, documentPercent: Math.min(100, documents) } }) }).catch(() => this.setData({loadState:'error',user:{}})) },
  formatStorage(bytes: number) { if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`; return `${Math.max(0, bytes / 1024 / 1024).toFixed(2)} MB` },
  // 会员方案是全屏弹层：tabBar 是原生组件，会压在弹层上方遮住底部按钮，打开时先收起
  setTabBarHidden(hidden: boolean) {
    if (typeof this.getTabBar !== 'function') return
    const bar = this.getTabBar()
    if (bar && typeof bar.setData === 'function') bar.setData({ hidden })
  },
  payPro() {
    this.setTabBarHidden(true)
    this.setData({ planPickerVisible: true, visiblePlans: this.data.plans.filter((item: any) => item.tier === this.data.selectedTier) })
  },
  // 使用技巧完全由后端下发：这里只负责取列表，并把相对资源路径拼成完整地址。
  loadTips() {
    getTipsIndex().then((index) => {
      const groups = (index.groups || []).map((group: any) => ({
        ...group,
        entries: (group.entries || []).map((entry: any) => ({ ...entry, cover: contentAssetUrl(entry.cover) })),
      }))
      this.setData({ tipGroups: groups, tipsLoading: false, tipsFailed: false })
      const covers = groups.reduce((all: string[], group: any) => all.concat(
        (group.entries || []).map((entry: any) => entry.cover).filter(Boolean),
      ), [])
      // 取不到的封面写成空串，列表里直接不显示缩略图，不留灰框
      localizeImages(covers).then((map) => this.setData({
        tipGroups: groups.map((group: any) => ({
          ...group,
          entries: (group.entries || []).map((entry: any) => (entry.cover ? { ...entry, cover: applyLocalized(entry.cover, map) } : entry)),
        })),
      })).catch(() => {})
    }).catch(() => this.setData({ tipsLoading: false, tipsFailed: true }))
  },
  // 会员方案与价格以后端 /api/pay/plans 为准；取不到时保留本地兜底价格，不影响下单
  loadPlans() {
    getPayPlans().then((catalog) => {
      const tiers = catalog.tiers || []
      const plans: any[] = []
      const tierDesc: any = {}
      const tierBenefit: any = {}
      tiers.forEach((tier) => {
        tierDesc[tier.id] = `${tier.knowledge_bases} 个资料库 · ${tier.storage_label}`
        tierBenefit[tier.id] = `每月 ${tier.monthly_questions} 次问答 · 单文件 ${tier.max_file_label}`
        const monthly = (tier.plans || []).find((plan) => plan.days <= 31)
        ;(tier.plans || []).forEach((plan) => {
          const months = plan.days <= 31 ? 1 : plan.days <= 100 ? 3 : 12
          const label = months === 1 ? '月付' : months === 3 ? '季付' : '年付'
          let note = '按需开通'
          if (monthly && months > 1 && monthly.amount > 0) {
            const saved = Math.round((1 - plan.amount / (monthly.amount * months)) * 100)
            if (saved >= 3) note = `省 ${saved}%`
          }
          plans.push({ id: plan.id, tier: tier.id, label, price: plan.price, period: `${plan.days} 天`, note })
        })
      })
      if (!plans.length) return
      const monthlyPrice = (tierId: string) => {
        const found = plans.find((item) => item.tier === tierId && item.label === '月付')
        return found ? found.price : ''
      }
      const selected = plans.find((item) => item.id === this.data.selectedPlan)
        || plans.find((item) => item.tier === this.data.selectedTier && item.label === '季付')
        || plans.find((item) => item.tier === this.data.selectedTier)
      this.setData({
        plans, tierDesc, tierBenefit,
        membershipCopy: `Plus ¥${monthlyPrice('plus')}/月 · Pro ¥${monthlyPrice('pro')}/月`,
        visiblePlans: plans.filter((item) => item.tier === this.data.selectedTier),
        activeBenefit: tierBenefit[this.data.selectedTier] || this.data.activeBenefit,
        selectedPlan: (selected ? selected.id : this.data.selectedPlan) as MembershipPlan,
        selectedPlanPrice: selected ? selected.price : this.data.selectedPlanPrice,
        selectedPlanPeriod: selected ? selected.period : this.data.selectedPlanPeriod,
      })
    }).catch(() => { /* 后端不可用时继续用本地兜底价格 */ })
  },
  // 使用技巧是「放在那里、用户自己点开自己看」的内容：
  // 只允许由用户在本页列表里点某一条进入，任何业务流程（启动、上传、问答、空状态）
  // 都不准自动跳转或弹出使用技巧页，也不要弹提示催促用户去看。
  openTip(e: any) {
    const id = e.currentTarget.dataset.id
    if (id) wx.navigateTo({ url: `/pages/tips/index?id=${id}` })
  },
  closePlanPicker() { if (this.data.paying) return; this.setTabBarHidden(false); this.setData({ planPickerVisible: false }) },
  stopPickerBubble() { return },
  chooseTier(e: any) {
    if (this.data.paying) return
    const tier = e.currentTarget.dataset.tier as 'plus' | 'pro'
    const selected = this.data.plans.find((item: any) => item.tier === tier && item.id.endsWith('quarterly')) || this.data.plans.find((item: any) => item.tier === tier)
    if (!selected) return
    this.setData({ selectedTier: tier, selectedPlan: selected.id as MembershipPlan, selectedPlanPrice: selected.price, selectedPlanPeriod: selected.period, activeBenefit: this.data.tierBenefit[tier] || this.data.activeBenefit, visiblePlans: this.data.plans.filter((item: any) => item.tier === tier) })
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
      this.setTabBarHidden(false)
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
