import { getKnowledge } from '../../services/api'

Page({
  data: { type: 'privacy', title: '隐私与安全' },
  onLoad(query: any) {
    const type = query.type || 'privacy'
    const titles: Record<string, string> = {
      data: '数据管理',
      privacy: '隐私与安全',
      agreement: '用户服务协议',
      plan: '权益说明',
      about: '关于 cola 知识库',
    }
    this.setData({ type, title: titles[type] || titles.privacy })
  },
  back() { wx.navigateBack() },
  copyDataSummary() {
    getKnowledge().then((knowledge) => {
      const data = {
        exported_at: new Date().toISOString(),
        knowledge: knowledge.map(item => ({
          name: item.name,
          description: item.description,
          documents: item.document_count,
          updated_at: item.updated_at,
        })),
      }
      wx.setClipboardData({ data: JSON.stringify(data, null, 2), success: () => wx.showToast({ title: '已复制清单', icon: 'success' }) })
    }).catch(() => wx.showToast({ title: '导出失败，请稍后重试', icon: 'none' }))
  },
  clearLocalCache() {
    wx.removeStorageSync('llmwiki_user')
    wx.showToast({ title: '本地缓存已清理', icon: 'success' })
  },
})
