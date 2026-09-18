import { getKnowledge, getMe } from '../../../services/api'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'

Page({
  data: {
    loadState: 'loading',
    user: {} as any,
    stats: { knowledge: 0, documents: 0, knowledgeLimit: 1 },
    storageUsedLabel: '0.00 MB',
    storageLimitLabel: '300 MB',
    storageFillStyle: 'width:0%;',
    quotaLabel: '本月积分 0 / 600',
    quotaPercent: 0,
    accountState: '正在读取使用情况',
  },
  onLoad() { this.load() },
  load() {
    this.setData({ loadState: 'loading' })
    Promise.all([getMe(), getKnowledge()]).then((pair) => {
      const user = pair[0]
      const knowledge = pair[1]
      const defaultIndex = knowledge.findIndex((item: any) => item.name === DEFAULT_KNOWLEDGE_NAME)
      const orderedKnowledge = defaultIndex > 0
        ? [knowledge[defaultIndex], ...knowledge.slice(0, defaultIndex), ...knowledge.slice(defaultIndex + 1)]
        : knowledge
      const documents = user.usage?.documents ?? orderedKnowledge.reduce((sum: number, item: any) => sum + (item.document_count || 0), 0)
      const storageUsed = Number(user.usage?.storage_bytes || 0)
      const storageLimit = Number(user.limits?.storage_bytes || 300 * 1024 * 1024)
      const storagePercent = storageLimit ? Math.min(100, Math.round(storageUsed / storageLimit * 100)) : 0
      const quota = user.quota || { credits_limit: 600, credits_used: 0 }
      const quotaPercent = quota.credits_limit ? Math.min(100, Math.round(Number(quota.credits_used || 0) / Number(quota.credits_limit) * 100)) : 0
      const trial = user.trial || { active: false, days_left: 0 }
      const period = user.period || { days_left: 0 }
      const member = Boolean(user.entitlements && user.entitlements.member)
      const accountState = member
        ? `${user.membership_label || '会员'} · 租期剩余 ${period.days_left} 天`
        : (trial.active ? `免费试用剩余 ${trial.days_left} 天` : '免费试用已结束 · 资料仍可查看')
      this.setData({
        loadState: 'ready',
        user,
        stats: {
          knowledge: user.usage?.knowledge_bases ?? orderedKnowledge.length,
          documents,
          knowledgeLimit: user.limits?.knowledge_bases || 1,
        },
        storageUsedLabel: this.formatStorage(storageUsed),
        storageLimitLabel: this.formatStorage(storageLimit),
        storageFillStyle: `width:${storagePercent}%;`,
        quotaLabel: `本月积分 ${quota.credits_used || 0} / ${quota.credits_limit || 0}`,
        quotaPercent,
        accountState,
      })
    }).catch(() => this.setData({ loadState: 'error' }))
  },
  formatStorage(bytes: number) {
    if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`
    return `${Math.max(0, bytes / 1024 / 1024).toFixed(2)} MB`
  },
  retry() { this.load() },
})
