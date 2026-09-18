import { getMe, getPayPlans, MembershipPlan, payPlan } from '../../../services/api'

Page({
  data: {
    loadState: 'loading',
    user: {} as any,
    membershipState: '',
    selectedTier: 'pro' as 'plus' | 'pro',
    selectedPlan: 'pro_quarterly' as MembershipPlan,
    selectedPlanPrice: '75.00',
    selectedPlanPeriod: '92 天',
    plans: [] as any[],
    visiblePlans: [] as any[],
    tierDesc: {} as Record<string, string>,
    tierBenefit: {} as Record<string, string>,
    activeBenefit: '',
    activeTierDesc: '',
    paying: false,
  },
  onLoad() { this.load() },
  load() {
    this.setData({ loadState: 'loading' })
    const plansReady = getPayPlans().catch(() => ({ tiers: [] } as any))
    Promise.all([getMe(), plansReady]).then((pair) => {
      const user = pair[0]
      const catalog = pair[1]
      const tiers = catalog.tiers || []
      const plans: any[] = []
      const tierDesc: Record<string, string> = {
        plus: '10 个资料库 · 10GB',
        pro: '50 个资料库 · 30GB',
      }
      const tierBenefit: Record<string, string> = {
        plus: '每月 3,000 积分 · 单文件 100MB',
        pro: '每月 15,000 积分 · 单文件 300MB',
      }
      tiers.forEach((tier: any) => {
        tierDesc[tier.id] = `${tier.knowledge_bases} 个资料库 · ${tier.storage_label}`
        tierBenefit[tier.id] = `每月 ${tier.monthly_credits} 积分 · 单文件 ${tier.max_file_label}`
        const monthly = (tier.plans || []).find((plan: any) => plan.days <= 31)
        ;(tier.plans || []).forEach((plan: any) => {
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
      const fallbackPlans = plans.length ? plans : [
        { id: 'plus_monthly', tier: 'plus', label: '月付', price: '12.00', period: '31 天', note: '按需开通' },
        { id: 'plus_quarterly', tier: 'plus', label: '季付', price: '30.00', period: '92 天', note: '省 17%' },
        { id: 'plus_yearly', tier: 'plus', label: '年付', price: '108.00', period: '365 天', note: '省 25%' },
        { id: 'pro_monthly', tier: 'pro', label: '月付', price: '29.00', period: '31 天', note: '按需开通' },
        { id: 'pro_quarterly', tier: 'pro', label: '季付', price: '75.00', period: '92 天', note: '省 14%' },
        { id: 'pro_yearly', tier: 'pro', label: '年付', price: '258.00', period: '365 天', note: '省 26%' },
      ]
      const selected = fallbackPlans.find((item: any) => item.id === this.data.selectedPlan)
        || fallbackPlans.find((item: any) => item.tier === this.data.selectedTier && item.label === '季付')
        || fallbackPlans.find((item: any) => item.tier === this.data.selectedTier)
      const trial = user.trial || { active: false, days_left: 0 }
      const period = user.period || { days_left: 0 }
      const member = Boolean(user.entitlements && user.entitlements.member)
      const membershipState = member
        ? `${user.membership_label || '会员'} · 租期剩余 ${period.days_left} 天`
        : (trial.active ? `免费试用剩余 ${trial.days_left} 天` : '免费试用已结束')
      this.setData({
        loadState: 'ready',
        user,
        membershipState,
        plans: fallbackPlans,
        visiblePlans: fallbackPlans.filter((item: any) => item.tier === this.data.selectedTier),
        tierDesc,
        tierBenefit,
        activeBenefit: tierBenefit[this.data.selectedTier] || '',
        activeTierDesc: tierDesc[this.data.selectedTier] || '',
        selectedPlan: (selected ? selected.id : this.data.selectedPlan) as MembershipPlan,
        selectedPlanPrice: selected ? selected.price : this.data.selectedPlanPrice,
        selectedPlanPeriod: selected ? selected.period : this.data.selectedPlanPeriod,
      })
    }).catch(() => this.setData({ loadState: 'error' }))
  },
  retry() { this.load() },
  chooseTier(e: any) {
    if (this.data.paying) return
    const tier = e.currentTarget.dataset.tier as 'plus' | 'pro'
    const selected = this.data.plans.find((item: any) => item.id === this.data.selectedPlan && item.tier === tier)
      || this.data.plans.find((item: any) => item.tier === tier && item.label === '季付')
      || this.data.plans.find((item: any) => item.tier === tier)
    this.setData({
      selectedTier: tier,
      activeBenefit: this.data.tierBenefit[tier] || this.data.activeBenefit,
      activeTierDesc: this.data.tierDesc[tier] || '',
      visiblePlans: this.data.plans.filter((item: any) => item.tier === tier),
      selectedPlan: selected ? selected.id : this.data.selectedPlan,
      selectedPlanPrice: selected ? selected.price : this.data.selectedPlanPrice,
      selectedPlanPeriod: selected ? selected.period : this.data.selectedPlanPeriod,
    })
  },
  choosePlan(e: any) {
    if (this.data.paying) return
    const id = e.currentTarget.dataset.plan as MembershipPlan
    const selected = this.data.visiblePlans.find((item: any) => item.id === id)
    if (selected) this.setData({ selectedPlan: id, selectedPlanPrice: selected.price, selectedPlanPeriod: selected.period })
  },
  async confirmPay() {
    if (this.data.paying) return
    this.setData({ paying: true })
    try {
      wx.showLoading({ title: '正在创建订单', mask: true })
      await payPlan(this.data.selectedPlan)
      wx.hideLoading()
      wx.showToast({ title: '支付成功', icon: 'success' })
      this.load()
    } catch (error: any) {
      wx.hideLoading()
      if (!error?.errMsg?.includes('cancel') && !error?.cancelled) wx.showToast({ title: error?.message || '支付暂不可用', icon: 'none' })
    } finally {
      this.setData({ paying: false })
    }
  },
  openPlanTerms() { wx.navigateTo({ url: '/package-features/pages/legal/index?type=plan' }) },
})
