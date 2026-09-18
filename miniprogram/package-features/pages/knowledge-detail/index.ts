import { openChat } from '../../../services/navigation'
import { warnPrivacyRequired } from '../../../services/privacy'
import { deleteDocument, deleteKnowledge, getKnowledge, getKnowledgeDetail, retryDocument, updateDocumentTags, uploadDocument, UploadSource } from '../../../services/api'
import { fileTypeKind, fileTypeLabel } from '../../../utils/file-type'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'

Page({
  data: {
    id: '',
    knowledge: {} as any,
    documents: [] as any[],
    loadState: 'loading',
    uploading: false,
    completedCount: 0,
    pendingCount: 0,
    failedCount: 0,
    // 好友共享过来的知识库：只读，隐藏上传 / 删除入口（后端也会把写入拦下来）
    readOnly: false,
    uploadSheetVisible: false,
    uploadUsedLabel: '0.00GB',
    uploadLimitLabel: '300MB',
    docTouchStartX: 0,
    docTouchStartY: 0,
  },
  onLoad(query: any) {
    const id = (query && query.id) || ''
    // 正常入口（知识库 tab、搜索结果、分享落地页）都会带 ?id=。
    // 但扫码 / 开发者工具直接指定页面等场景可能没有参数，这里回退到默认资料库，
    // 拿不到再回「知识库」tab，避免停在「资料库加载失败」。
    if (id) {
      this.setData({ id })
      this.load()
      return
    }
    getKnowledge().then((items) => {
      const list = (items || []) as any[]
      const fallback = list.find((item: any) => item.name === DEFAULT_KNOWLEDGE_NAME) || list[0]
      if (!fallback) throw new Error('empty')
      this.setData({ id: fallback.id })
      this.load()
    }).catch(() => wx.switchTab({ url: '/pages/chat/index', fail: () => wx.navigateBack({ fail: () => {} }) }))
  },
  load() {
    this.setData({ loadState: 'loading' })
    getKnowledgeDetail(this.data.id).then((data) => {
      const documents = data.documents.map((doc: any) => ({
        ...doc,
        swipeX: 0,
        typeKind: fileTypeKind(doc.file_type),
        typeLabel: fileTypeLabel(doc.file_type),
        displaySize: this.formatSize(doc.file_size),
        displayTime: this.displayTime(doc.created_at || doc.updated_at),
      }))
      const usedBytes = documents.reduce((sum: number, doc: any) => sum + Number(doc.file_size || 0), 0)
      this.setData({
        ...data,
        readOnly: !!((data as any).read_only || ((data as any).knowledge && (data as any).knowledge.read_only)),
        loadState: 'ready',
        completedCount: data.documents.filter(d => d.status === 'completed').length,
        pendingCount: data.documents.filter(d => d.status !== 'completed' && d.status !== 'failed').length,
        failedCount: data.documents.filter(d => d.status === 'failed').length,
        uploadUsedLabel: this.formatStorage(usedBytes),
        documents,
      })
    }).catch(() => this.setData({ loadState: 'error' }))
  },
  back() { wx.navigateBack() },
  goChat() { openChat({ knowledgeId: this.data.id }) },
  goSearch() { wx.navigateTo({ url: `/package-features/pages/search/index?knowledgeId=${this.data.id}` }) },
  switchKnowledge() {
    getKnowledge().then((items) => {
      if (!items.length) {
        wx.showToast({ title: '暂无其他知识库', icon: 'none' })
        return
      }
      wx.showActionSheet({
        itemList: items.map(item => item.name),
        success: ({ tapIndex }) => {
          const item = items[tapIndex]
          if (item && item.id !== this.data.id) wx.redirectTo({ url: `/package-features/pages/knowledge-detail/index?id=${item.id}` })
        },
      })
    }).catch(() => wx.showToast({ title: '知识库加载失败', icon: 'none' }))
  },
  activate() { wx.switchTab({ url: '/pages/mine/index' }) },
  showActions() {
    // 好友共享过来的库只能「移除」，不能删到对方那边去
    const deleteLabel = this.data.readOnly ? '移除这个共享知识库' : '删除资料库'
    wx.showActionSheet({
      itemList: ['资料问答', deleteLabel],
      success: async ({ tapIndex }) => {
        if (tapIndex === 0) this.goChat()
        if (tapIndex === 1) {
          const confirm = await new Promise<any>((resolve) => wx.showModal({
            title: this.data.readOnly ? '移除共享知识库' : '删除资料库',
            content: this.data.readOnly ? '移除后这里不再显示它，对方的资料不受影响。' : '删除后其中的文档、切片和问答记录都无法恢复。',
            confirmColor: '#c62828',
            success: resolve,
          }))
          if (!confirm.confirm) return
          try {
            await deleteKnowledge(this.data.id)
            wx.showToast({ title: '已删除', icon: 'success' })
            setTimeout(() => wx.navigateBack(), 500)
          } catch (error: any) {
            wx.showToast({ title: error.message || '删除失败', icon: 'none' })
          }
        }
      },
    })
  },
  async upload() {
    if (this.data.readOnly) { wx.showToast({ title: '好友共享的知识库只能阅读和提问', icon: 'none' }); return }
    this.openUploadSheet()
  },
  openUploadSheet() {
    if (this.data.uploading) return
    this.setData({ uploadSheetVisible: true })
  },
  closeUploadSheet() {
    if (!this.data.uploading) this.setData({ uploadSheetVisible: false })
  },
  // 相册、拍照、微信文件都是隐私接口：上传前先给一个随时可读的《小程序用户隐私保护指引》入口。
  openPrivacyGuide() { wx.navigateTo({ url: '/package-features/pages/legal/index?type=guide' }) },
  async chooseUploadSource(e: any) {
    const source = String(e.currentTarget.dataset.source || '')
    if (!source || this.data.uploading) return
    if (source === 'knowledge') {
      this.setData({ uploadSheetVisible: false })
      this.switchKnowledge()
      return
    }
    if (source === 'folder') {
      wx.showToast({ title: '文件夹用于整理资料，暂不需要创建', icon: 'none' })
      return
    }
    if (this.data.uploading) return
    this.setData({ uploading: true, uploadSheetVisible: false })
    try {
      const result = await uploadDocument(this.data.id, source as UploadSource)
      wx.showToast({ title: result.status === 'completed' ? '已上传并完成解析' : '已上传，正在解析', icon: result.status === 'completed' ? 'success' : 'none' })
      this.load()
    } catch (error: any) {
      if (error?.cancelled || String(error?.errMsg || error?.message || '').includes('cancel')) return
      if (error?.privacy) { warnPrivacyRequired('导入资料'); return }
      wx.showToast({ title: error?.message || '上传失败，请稍后重试', icon: 'none' })
    } finally {
      this.setData({ uploading: false })
    }
  },
  stopSheetBubble() { return },
  viewDoc(e: any) {
    const doc = this.data.documents.find((item: any) => item.id === e.currentTarget.dataset.id)
    if (doc && Number(doc.swipeX || 0) !== 0) {
      this.closeDocSwipe()
      return
    }
    wx.navigateTo({ url: `/package-features/pages/document/index?id=${e.currentTarget.dataset.id}` })
  },
  onDocTouchStart(e: any) {
    this.setData({
      docTouchStartX: Number(e.touches?.[0]?.clientX || 0),
      docTouchStartY: Number(e.touches?.[0]?.clientY || 0),
    })
  },
  onDocTouchMove(e: any) {
    const index = Number(e.currentTarget.dataset.index)
    const touch = e.touches?.[0]
    if (!touch || index < 0) return
    const dx = Number(touch.clientX || 0) - Number(this.data.docTouchStartX || 0)
    const dy = Number(touch.clientY || 0) - Number(this.data.docTouchStartY || 0)
    if (Math.abs(dy) > Math.abs(dx) || Math.abs(dx) < 8) return
    const swipeX = Math.max(-232, Math.min(0, Math.round(dx)))
    this.setData({ documents: this.data.documents.map((doc: any, i: number) => ({ ...doc, swipeX: i === index ? swipeX : 0 })) })
  },
  onDocTouchEnd(e: any) {
    const index = Number(e.currentTarget.dataset.index)
    const doc = this.data.documents[index]
    if (!doc) return
    const swipeX = Number(doc.swipeX || 0) <= -100 ? -232 : 0
    this.setData({ documents: this.data.documents.map((item: any, i: number) => ({ ...item, swipeX: i === index ? swipeX : 0 })) })
  },
  closeDocSwipe() {
    this.setData({ documents: this.data.documents.map((item: any) => ({ ...item, swipeX: 0 })) })
  },
  tagDocument(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    if (!id) return
    ;(wx.showModal as any)({
      title: '添加标签',
      editable: true,
      placeholderText: '例如：项目资料',
      confirmText: '保存',
      success: async (result: any) => {
        if (!result.confirm) return
        const tags = String(result.content || '').split(/[，,\s]+/).map(item => item.trim()).filter(Boolean)
        if (!tags.length) {
          wx.showToast({ title: '请输入标签', icon: 'none' })
          return
        }
        try {
          await updateDocumentTags(id, tags)
          wx.showToast({ title: '标签已保存', icon: 'success' })
          this.closeDocSwipe()
          this.load()
        } catch (error: any) {
          wx.showToast({ title: error.message || '标签保存失败', icon: 'none' })
        }
      },
    })
  },
  async confirmDeleteDocument(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    if (!id) return
    const confirm = await new Promise<any>((resolve) => wx.showModal({
      title: '删除文档',
      content: '删除后文档、索引和整理结果都无法恢复，确定继续吗？',
      confirmColor: '#d93025',
      success: resolve,
    }))
    if (!confirm.confirm) {
      this.closeDocSwipe()
      return
    }
    try {
      await deleteDocument(id)
      wx.showToast({ title: '已删除', icon: 'success' })
      this.load()
    } catch (error: any) {
      wx.showToast({ title: error.message || '删除失败', icon: 'none' })
    }
  },
  formatSize(bytes: number) {
    if (!bytes) return '0 KB'
    return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
  },
  formatStorage(bytes: number) {
    const value = Number(bytes || 0)
    if (value >= 1024 * 1024 * 1024) return `${(value / 1024 / 1024 / 1024).toFixed(2)}GB`
    return `${(value / 1024 / 1024 / 1024).toFixed(2)}GB`
  },
  displayTime(value: string) {
    const match = String(value || '').match(/T(\d{2}:\d{2})/)
    return match ? match[1] : '刚刚'
  },
  async retry(e: any) {
    try {
      await retryDocument(e.currentTarget.dataset.id)
      wx.showToast({ title: '已重新解析', icon: 'success' })
      this.load()
    } catch (error: any) {
      wx.showToast({ title: error.message || '重新解析失败', icon: 'none' })
    }
  },
  showDocumentActions(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    if (!id) return
    wx.showActionSheet({
      itemList: ['打开文档', '添加标签', '删除文档'],
      success: ({ tapIndex }) => {
        if (tapIndex === 0) wx.navigateTo({ url: `/package-features/pages/document/index?id=${id}` })
        if (tapIndex === 1) this.tagDocument({ currentTarget: { dataset: { id } } } as any)
        if (tapIndex === 2) this.confirmDeleteDocument({ currentTarget: { dataset: { id } } } as any)
      },
    })
  },
})
