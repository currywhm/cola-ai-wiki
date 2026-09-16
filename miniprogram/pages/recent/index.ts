import { getRecent } from '../../services/api'
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
    recentKbs: [] as KbItem[],
    recentDocs: [] as DocItem[],
  },
  onShow() {
    this.syncTabBar()
    this.loadRecent()
  },
  syncTabBar() {
    if (typeof this.getTabBar !== 'function') return
    const bar = this.getTabBar() as any
    if (bar && typeof bar.setData === 'function') bar.setData({ selected: 2, hidden: false })
  },
  loadRecent() {
    getRecent().then((data) => {
      const kbs = (data && data.knowledge) || []
      const items = (data && data.items) || []
      this.setData({
        loading: false,
        failed: false,
        recentKbs: kbs.slice(0, 8).map((item: any, index: number) => ({
          id: String(item.id || ''),
          name: String(item.name || '未命名知识库'),
          timeLabel: relativeTime(item.updated_at || item.created_at || ''),
          tone: KB_TONES[index % KB_TONES.length],
        })),
        recentDocs: items.slice(0, 30).map((item: any) => ({
          id: String(item.id || ''),
          kind: String(item.kind || 'document'),
          name: String(item.name || '未命名文件'),
          timeLabel: relativeTime(item.updated_at || ''),
          iconName: iconFor(String(item.kind || ''), String(item.file_type || '')),
          knowledgeId: String(item.knowledge_id || ''),
        })),
      })
    }).catch(() => {
      this.setData({ loading: false, failed: true })
    })
  },
  openKnowledge(e: any) {
    const id = e.currentTarget.dataset.id
    if (!id) return
    // 统一走共享导航：storage 里的旧键没人消费，跳过去会丢失目标知识库
    openChat({ knowledgeId: String(id) })
  },
  // 最近列表同时含文档与文件夹：文件夹不是文档，不能直接进文档预览页
  // （否则后端查不到该 id 的文档，只会弹「文档不存在」）。
  openItem(e: any) {
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
    if (!id) return
    wx.navigateTo({ url: `/pages/document/index?id=${id}` })
  },
})
