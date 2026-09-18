// 服务说明页：关于 / 数据管理 / 隐私安全 / 小程序隐私保护指引 / AI 隐私政策 /
// 用户服务协议 / 软件许可及服务协议 / 会员服务条款，正文全部由后端下发
// （server/content/legal → scripts/import_content.py → 数据库 → /api/content/legal）。
//
// 为什么不在前端写死文案：
// * 合规文案要能单独修改、单独评审，改完不需要重新提交小程序审核；
// * 条款里的价格、容量、额度必须和后端真实配置一致，写死在两边迟早对不上；
// * 运营者名称、备案号、联系邮箱随部署环境变化，由后端导入时替换占位符。
import { getKnowledge, getLegalDoc, getLegalIndex } from '../../../services/api'

const CACHE_KEY = 'cola_legal_cache_v1'
// 按 type 打开；万一传了未知 type，退回隐私保护指引，绝不出现空白页
const FALLBACK_ID = 'guide'
const KNOWN_IDS = ['about', 'data', 'privacy', 'guide', 'ai-privacy', 'terms', 'license', 'plan']

type CachedDoc = { id: string; title: string; summary: string; updated_at: string; effective_at: string; blocks: any[] }
type Cache = { version: string; docs: Record<string, CachedDoc> }

function readCache(): Cache {
  try {
    const value = wx.getStorageSync(CACHE_KEY)
    if (value && typeof value === 'object' && value.docs) return value as Cache
  } catch { /* 缓存损坏时当作没有缓存 */ }
  return { version: '', docs: {} }
}

function writeCache(cache: Cache) {
  try { wx.setStorageSync(CACHE_KEY, cache) } catch { /* 存储写满不影响阅读 */ }
}

// block 列表要带稳定 key，否则列表滚动时小程序会复用错节点
// 首块不再留上边距：WXSS 编译器不支持 `> :first-child` 这类选择器（编译直接报错），
// 所以由数据侧标记首块，样式用 .legal-first 处理。
const withKeys = (blocks: any[]) => (blocks || []).map((block: any, index: number) => ({
  ...block,
  key: `${block.type}-${index}`,
  first: index === 0,
  runs: (block.runs || []).map((run: any, runIndex: number) => ({ ...run, key: `${index}-${runIndex}` })),
}))

Page({
  data: {
    loadState: 'loading',
    docId: '',
    title: '服务说明',
    doc: {} as any,
    blocks: [] as any[],
    related: [] as any[],
    updatedLabel: '',
    docUpdatedLabel: '',
    docEffectiveLabel: '',
    isData: false,
    fromCache: false,
    copied: false,
    // 客服入口的三个参数：来源标记 / 会话内卡片标题 / 卡片点回小程序的路径
    contactSessionFrom: '',
    contactCardTitle: '',
    contactCardPath: '',
  },
  onLoad(query: any) {
    const raw = String(query?.type || query?.id || '')
    const docId = KNOWN_IDS.indexOf(raw) >= 0 ? raw : FALLBACK_ID
    this.setData({ docId, isData: docId === 'data' })
    // 客服会话的来源标记：客服在会话详情里能直接看到用户是点了哪份文档进来的
    this.setData({
      contactSessionFrom: `legal-${docId}`,
      contactCardTitle: '回到这份说明',
      contactCardPath: `package-features/pages/legal/index?type=${docId}`,
    })
    this.load(docId)
  },
  // 先渲染本地缓存，再拉后端。这样弱网或后端临时不可用时，用户仍然读得到条款原文。
  load(docId: string, options: { silent?: boolean } = {}) {
    if (!options.silent) this.setData({ loadState: 'loading' })
    const applyDetail = (detail: CachedDoc, related: any[], fromCache: boolean) => {
      this.setData({
        loadState: 'ready',
        doc: detail,
        title: detail.title || '服务说明',
        blocks: withKeys(detail.blocks),
        related,
        updatedLabel: [detail.updated_at ? `更新日期 ${detail.updated_at}` : '', detail.effective_at ? `生效日期 ${detail.effective_at}` : ''].filter(Boolean).join(' · '),
        docUpdatedLabel: detail.updated_at || '',
        docEffectiveLabel: detail.effective_at || '',
        fromCache,
      })
    }

    const cached = readCache()
    const cachedDoc = cached.docs[docId]
    if (cachedDoc && cachedDoc.blocks?.length) {
      applyDetail(cachedDoc, this.relatedFrom(cached, docId), true)
    }

    getLegalIndex()
      .then((index) => {
        const docs = index.docs || []
        const related = docs.filter((doc: any) => doc.id !== docId)
        return getLegalDoc(docId).then((detail: any) => {
          const next = readCache()
          next.version = index.version || next.version
          next.docs = { ...next.docs, [docId]: detail }
          writeCache(next)
          applyDetail(detail, related, false)
        })
      })
      .catch(() => {
        if (cachedDoc && cachedDoc.blocks?.length) return   // 已有缓存：静默降级，不打断阅读
        this.setData({ loadState: 'error' })
      })
  },
  relatedFrom(cache: Cache, docId: string) {
    const order = KNOWN_IDS.filter((id) => id !== docId)
    return order.map((id) => cache.docs[id]).filter(Boolean).slice(0, 3)
  },
  retry() { this.load(this.data.docId) },
  openDoc(e: any) {
    const id = e.currentTarget.dataset.id
    if (!id || id === this.data.docId) return
    wx.redirectTo({ url: `/package-features/pages/legal/index?type=${id}` })
  },
  // 客服会话：窗口由微信自己的 open-type="contact" 拉起，前端既不渲染聊天内容也不存聊天记录
  // （客服消息走服务端回调，见后端 /api/wechat/kf/callback）。
  // 这里只做两件事：记一条「从哪份文档进的客服」本地埋点；用户从会话卡片带着参数回来时按需跳页。
  onContact(e: any) {
    const detail = (e && e.detail) || {}
    try {
      wx.setStorageSync('cola_kf_entry', { from: this.data.contactSessionFrom, at: Date.now() })
    } catch { /* 埋点写不进去不影响使用 */ }
    const path = String(detail.path || '')
    const query = String(detail.query || '')
    // 卡片指回当前这份文档时不重复入栈，否则用户连点几次会攒一堆同样的页面
    if (!path || path.indexOf('/package-features/pages/legal/index') === 0) return
    wx.navigateTo({ url: `/${path.replace(/^\//, '')}${query ? `?${query}` : ''}`, fail: () => {} })
  },
  // 数据管理页的两个动作：导出一份资料库清单留档、清掉本机缓存的登录状态
  copyDataSummary() {
    getKnowledge().then((knowledge) => {
      const payload = {
        exported_at: new Date().toISOString(),
        knowledge: knowledge.map((item) => ({
          name: item.name,
          description: item.description,
          documents: item.document_count,
          updated_at: item.updated_at,
        })),
      }
      wx.setClipboardData({
        data: JSON.stringify(payload, null, 2),
        success: () => {
          this.setData({ copied: true })
          wx.showToast({ title: '清单已复制', icon: 'success' })
        },
      })
    }).catch((error: any) => {
      wx.showToast({ title: error?.statusCode === 401 ? '请先微信登录' : '导出失败，请稍后重试', icon: 'none' })
    })
  },
  clearLocalCache() {
    wx.showModal({
      title: '清理本地缓存',
      content: '只会清除这台设备上保存的登录状态和偏好设置，服务端的资料、对话和会员权益不受影响。清除后需要重新用微信登录。',
      confirmText: '清理',
      success: (result) => {
        if (!result.confirm) return
        wx.removeStorageSync('llmwiki_token')
        wx.removeStorageSync('llmwiki_user')
        wx.removeStorageSync(CACHE_KEY)
        const app = getApp<IAppOption>()
        app.globalData.token = ''
        app.globalData.user = null
        app.globalData.loggedOut = true
        wx.setStorageSync('llmwiki_logged_out', true)
        wx.showToast({ title: '本地缓存已清理', icon: 'success' })
        setTimeout(() => wx.reLaunch({ url: '/pages/login/index' }), 300)
      },
    })
  },
  back() { wx.navigateBack() },
  onShareAppMessage() {
    return { title: this.data.title, path: `/package-features/pages/legal/index?type=${this.data.docId}` }
  },
})
