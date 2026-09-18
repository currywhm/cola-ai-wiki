import { getRecent, goLogin, isLoggedIn } from '../../services/api'
import { openChat } from '../../services/navigation'

interface KbItem {
  id: string
  name: string
  timeLabel: string
  tone: string
}

interface DocItem {
  id: string
  kind: string
  name: string
  timeLabel: string
  iconName: string
  knowledgeId: string
}

const KB_TONES = ['tone-blue', 'tone-green', 'tone-yellow', 'tone-purple']

// 最近知识：只展示最新的 20 条（服务端也按同一上限收敛）
const RECENT_DOC_LIMIT = 20

const IMAGE_TYPES = ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.heic']

function pad(value: number) {
  return String(value).padStart(2, '0')
}

// 「今天 07:11 / 昨天 22:03 / 2025-10-08」——与参考稿的显示规则保持一致
function relativeTime(value: string) {
  if (!value) return ''
  const time = new Date(String(value).replace(' ', 'T'))
  if (isNaN(time.getTime())) return String(value).slice(0, 10)
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const stamp = time.getTime()
  const clock = `${pad(time.getHours())}:${pad(time.getMinutes())}`
  if (stamp >= today) return `今天 ${clock}`
  if (stamp >= today - 86400000) return `昨天 ${clock}`
  const sameYear = time.getFullYear() === now.getFullYear()
  return sameYear ? `${pad(time.getMonth() + 1)}-${pad(time.getDate())}` : `${time.getFullYear()}-${pad(time.getMonth() + 1)}-${pad(time.getDate())}`
}

// 最近知识库的时间：有使用记录就写「最近使用」（排序也按它），
// 从没打开/提问过的库退回资料更新时间，不把两个含义写成同一句话。
function kbTimeLabel(item: any) {
  const used = String((item && item.last_used_at) || '')
  if (used) return `最近使用 ${relativeTime(used)}`
  return `更新于 ${relativeTime(String((item && (item.updated_at || item.created_at)) || ''))}`
}

function iconFor(kind: string, fileType: string) {
  if (kind === 'folder') return 'recent-folder'
  const suffix = String(fileType || '').toLowerCase()
  if (suffix === '.pdf') return 'recent-pdf'
  if (suffix === '.doc' || suffix === '.docx') return 'recent-doc'
  if (suffix === '.ppt' || suffix === '.pptx') return 'recent-ppt'
  if (suffix === '.xls' || suffix === '.xlsx' || suffix === '.csv') return 'recent-sheet'
  if (IMAGE_TYPES.indexOf(suffix) >= 0) return 'recent-image'
  return 'recent-text'
}

Page({
  data: {
    loading: true,
    failed: false,
    loggedOut: false,
    recentKbs: [] as KbItem[],
    recentDocs: [] as DocItem[],
  },
  onShow() {
    this.syncTabBar()
    if (!isLoggedIn()) {
      this.setData({ loading: false, failed: false, loggedOut: true, recentKbs: [], recentDocs: [] })
      return
    }
    this.setData({ loggedOut: false })
    this.loadRecent()
  },
  syncTabBar() {
    if (typeof this.getTabBar !== 'function') return
    const bar = this.getTabBar() as any
    if (bar && typeof bar.setData === 'function') bar.setData({ selected: 2, hidden: false })
  },
  loadRecent() {
    if (!isLoggedIn()) {
      this.setData({ loading: false, failed: false, loggedOut: true, recentKbs: [], recentDocs: [] })
      return
    }
    getRecent(RECENT_DOC_LIMIT).then((data) => {
      const kbs = (data && data.knowledge) || []
      const items = (data && data.items) || []
      this.setData({
        loading: false,
        failed: false,
        recentKbs: kbs.slice(0, 8).map((item: any, index: number) => ({
          id: String(item.id || ''),
          name: String(item.name || '未命名知识库'),
          timeLabel: kbTimeLabel(item),
          tone: KB_TONES[index % KB_TONES.length],
        })),
        recentDocs: items.slice(0, RECENT_DOC_LIMIT).map((item: any) => ({
          id: String(item.id || ''),
          kind: String(item.kind || 'document'),
          name: String(item.name || '未命名文件'),
          // 与排序口径一致：打开过用打开时间，没打开过用资料更新时间
          timeLabel: relativeTime(item.last_viewed_at || item.updated_at || ''),
          iconName: iconFor(String(item.kind || ''), String(item.file_type || '')),
          knowledgeId: String(item.knowledge_id || ''),
        })),
      })
    }).catch(() => {
      this.setData({ loading: false, failed: true })
    })
  },
  openKnowledge(e: any) {
    if (!isLoggedIn()) { goLogin(); return }
    const id = e.currentTarget.dataset.id
    if (!id) return
    // 统一走共享导航：storage 里的旧键没人消费，跳过去会丢失目标知识库
    openChat({ knowledgeId: String(id) })
  },
  // 最近列表同时含文档与文件夹：文件夹不是文档，不能直接进文档预览页
  // （否则后端查不到该 id 的文档，只会弹「文档不存在」）。
  openItem(e: any) {
    if (!isLoggedIn()) { goLogin(); return }
    const id = String(e.currentTarget.dataset.id || '')
    if (!id) return
    const kind = String(e.currentTarget.dataset.kind || 'document')
    const knowledgeId = String(e.currentTarget.dataset.knowledge || '')
    if (kind === 'folder') {
      if (!knowledgeId) {
        wx.showToast({ title: '文件夹所属知识库已不可用', icon: 'none' })
        return
      }
      openChat({ knowledgeId, folderId: id })
      return
    }
    this.openDocument(id)
  },
  openDocument(id: string) {
    if (!isLoggedIn()) { goLogin(); return }
    if (!id) return
    wx.navigateTo({ url: `/package-features/pages/document/index?id=${id}` })
  },
  login() { goLogin() },
})
