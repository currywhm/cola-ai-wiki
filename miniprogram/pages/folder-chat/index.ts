import { renderMarkdown } from '../../utils/markdown'
import { getConversation, getConversations, getKnowledgeDetail, getModels, ModelOption, Source, streamChat } from '../../services/api'

// 与后端 CHAT_MODELS 一致的兜底清单；正常运行时会被 /api/models 的返回值覆盖
const FALLBACK_MODEL_OPTIONS: ModelOption[] = [
  { id: 'deepseek-flash', name: '云枢', value: 'deepseek-flash', badge: '', short: '云枢' },
  { id: 'deepseek-v4-pro', name: '墨衡', value: 'deepseek-v4-pro', badge: '', short: '墨衡' },
]

Page({
  data: { safeBottom: 0, loadState: 'loading' as 'loading' | 'ready' | 'error', knowledgeId: '', knowledgeName: '', folderId: '', folderName: '', documents: [] as any[], documentsLoading: true, documentsError: false, conversationId: '', conversationActive: false, input: '', canSend: false, sending: false, readyForInput: false, lastMessageId: '', model: 'deepseek-flash', selectedModelKey: 'deepseek-flash', modelShortLabel: '云枢', thinkingMode: 'quick' as 'quick' | 'deep', modelOptions: FALLBACK_MODEL_OPTIONS, modelPickerVisible: false, messages: [] as any[] },
  onLoad(options: any) {
    const knowledgeId = String(options.knowledgeId || '')
    // query 参数可能经过 encodeURIComponent，必须解码，否则中文显示为 %XX 乱码
    const knowledgeName = decodeURIComponent(String(options.knowledgeName || ''))
    const folderId = String(options.folderId || '')
    const folderName = decodeURIComponent(String(options.folderName || ''))
    this.setData({ knowledgeId, knowledgeName, folderId, folderName })
    this.measureSafeArea()
    this.loadModels()
    this.loadFolder()
  },
  onShow() { this.measureSafeArea() },
  onUnload() { const cancel = (this as any).cancelStream; if (cancel) cancel() },
  // 与 chat 页一致：真机 env() 偶发失效，用实测 Home 条高度兜底（取不到走 CSS，不叠加）
  measureSafeArea() {
    try {
      const info = (wx as any).getWindowInfo ? (wx as any).getWindowInfo() : wx.getSystemInfoSync()
      const bottom = info && info.safeArea ? Math.max(0, (info.windowHeight || 0) - (info.safeArea.bottom || 0)) : 0
      if (bottom !== this.data.safeBottom) this.setData({ safeBottom: bottom })
    } catch (e) { /* 忽略，继续走 CSS env() 兜底 */ }
  },
  loadModels() {
    getModels().then((options) => {
      if (!options || !options.length) return
      const current = options.find((item) => item.id === this.data.selectedModelKey) || options[0]
      this.setData({ modelOptions: options, selectedModelKey: current.id, model: current.value, modelShortLabel: current.short })
    }).catch(() => {})
  },
  loadFolder() {
    if (!this.data.knowledgeId || !this.data.folderId) { this.setData({ loadState: 'error', documentsLoading: false, documentsError: true }); return }
    this.setData({ documentsLoading: true, documentsError: false })
    getKnowledgeDetail(this.data.knowledgeId).then((result: any) => {
      const folderDocs = ((result && result.documents) || []).filter((d: any) => d.folder_id === this.data.folderId)
      this.setData({
        documents: folderDocs.map((item: any) => ({
          ...item,
          typeKind: /^\.?(png|jpe?g|gif|webp|bmp|heic)$/i.test(String(item.file_type || '')) ? 'image' : item.file_type === '.pdf' ? 'pdf' : 'other',
          typeLabel: /^\.?(png|jpe?g|gif|webp|bmp|heic)$/i.test(String(item.file_type || '')) ? '图片' : item.file_type === '.pdf' ? 'PDF' : item.file_type === '.docx' ? 'DOC' : item.file_type === '.doc' ? 'DOC' : item.file_type === '.md' || item.file_type === '.markdown' ? 'MD' : item.file_type === '.txt' ? 'TXT' : item.file_type === '.html' ? '推文' : 'FILE',
          displayTime: this.displayTime(item.created_at || item.updated_at),
        })),
        documentsLoading: false,
        loadState: 'ready',
        readyForInput: true,
      }, () => this.syncCanSend())
    }).catch(() => this.setData({ documents: [], documentsLoading: false, documentsError: true, loadState: 'error' }))
  },
  displayTime(value: string) {
    if (!value) return ''
    const time = new Date(String(value).replace(' ', 'T'))
    if (isNaN(time.getTime())) return String(value).slice(5, 16)
    const now = new Date()
    const sameDay = time.toDateString() === now.toDateString()
    const pad = (n: number) => String(n).padStart(2, '0')
    return sameDay ? `${pad(time.getHours())}:${pad(time.getMinutes())}` : `${pad(time.getMonth() + 1)}-${pad(time.getDate())}`
  },
  // 恢复本文件夹最近一次会话；文件夹会话带 folder_id，与根目录会话天然隔离
  restoreLatestConversation() {
    getConversations().then((items) => {
      const latest = (items || []).find((item: any) => item.knowledge_id === this.data.knowledgeId && (item.folder_id || '') === this.data.folderId)
      if (!latest) return
      if (this.data.conversationActive || this.data.messages.length || this.data.input.trim()) return
      this.setData({ conversationId: latest.id, conversationActive: true })
      this.loadConversation()
    }).catch(() => undefined)
  },
  loadConversation() { getConversation(this.data.conversationId).then((messages) => { const hydrated = messages.map((m: any) => m.role === 'assistant' ? { ...m, html: renderMarkdown(m.content || ''), progress: '' } : m); this.setData({ messages: hydrated, lastMessageId: hydrated.length ? hydrated[hydrated.length - 1].id : '' }) }).catch(() => undefined) },
  buildGuideMessage() {
    const id = `guide-${Date.now()}`
    const kbName = this.data.knowledgeName || '微信用户的知识库'
    const folderName = this.data.folderName || '当前文件夹'
    const count = this.data.documents.length
    const content = count
      ? `Hi，这里是「${kbName}」的「${folderName}」文件夹，共 ${count} 个文件。关于这些文件的问题，都尽管问。`
      : `Hi，这里是「${kbName}」的「${folderName}」文件夹，当前还没有文件。`
    return { id, role: 'assistant', local: true, sources: [], content }
  },
  enterConversation() {
    if (this.data.conversationActive || this.data.sending || this.data.loadState !== 'ready') return
    const guide = this.buildGuideMessage()
    this.setData({ conversationActive: true, conversationId: '', messages: [guide], lastMessageId: guide.id })
  },
  onInput(e: any) {
    const next = { input: e.detail.value, readyForInput: true } as any
    if (!this.data.conversationActive) {
      const guide = this.buildGuideMessage()
      Object.assign(next, { conversationActive: true, conversationId: '', messages: [guide], lastMessageId: guide.id })
    }
    this.setData(next)
    this.syncCanSend()
  },
  syncCanSend() { this.setData({ canSend: !!this.data.readyForInput && !!this.data.input.trim() && !!this.data.knowledgeId && !!this.data.folderId && this.data.loadState === 'ready' && !this.data.sending }) },
  send() {
    const content = this.data.input.trim()
    if (!this.data.readyForInput || this.data.loadState !== 'ready' || !content || this.data.sending) return
    if (!this.data.knowledgeId || !this.data.folderId) { wx.showToast({ title: '文件夹信息缺失', icon: 'none' }); return }
    this.doSend(content)
  },
  doSend(content: string) {
    const userId = `m${Date.now()}`; const assistantId = `m${Date.now() + 1}`
    this.setData({ input: '', conversationActive: true, sending: true, canSend: false, messages: [...this.data.messages, { id: userId, role: 'user', content, sources: [] }, { id: assistantId, role: 'assistant', content: '', html: '', progress: '正在检索资料并组织回答…', sources: [] }], lastMessageId: assistantId })
    let assistant = ''
    const payload: any = { mode: 'knowledge', conversation_id: this.data.conversationId || undefined, folder_id: this.data.folderId, knowledge_id: this.data.knowledgeId, content, model: this.data.model, thinking: this.data.thinkingMode }
    ;(this as any).cancelStream = streamChat(payload, (meta) => { this.setData({ conversationId: meta.conversation_id }); this.updateAssistant(assistantId, assistant, meta.sources as Source[]) }, (delta) => { assistant += delta; this.updateAssistant(assistantId, assistant) }, () => { (this as any).cancelStream = null; this.setData({ sending: false }); this.syncCanSend() }, (error) => { (this as any).cancelStream = null; this.setData({ sending: false }); this.updateAssistant(assistantId, assistant || '回答未完成，请稍后重新提问。'); this.syncCanSend(); wx.showToast({ title: error.message || error.errMsg || '回答失败', icon: 'none' }) }, (label) => { if (label) this.updateAssistantProgress(assistantId, label) })
  },
  stopSend() {
    const cancel = (this as any).cancelStream
    if (cancel) { (this as any).cancelStream = null; cancel() }
    if (this.data.sending) { this.setData({ sending: false }, () => this.syncCanSend()) }
  },
  updateAssistant(id: string, content: string, sources?: Source[]) { const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, content, html: renderMarkdown(content), progress: content ? '' : message.progress, sources: sources || message.sources } : message); this.setData({ messages, lastMessageId: id }) },
  updateAssistantProgress(id: string, progress: string) { const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, progress } : message); this.setData({ messages }) },
  copyAnswer(e: any) {
    const content = String(e.currentTarget.dataset.content || '')
    if (!content) return
    wx.setClipboardData({ data: content, success: () => wx.showToast({ title: '回答已复制', icon: 'success' }) })
  },
  shareAnswer(e: any) {
    const content = String(e.currentTarget.dataset.content || '')
    if (content) wx.setClipboardData({ data: content, success: () => wx.showToast({ title: '回答已复制，可转发', icon: 'none' }) })
  },
  openSource(e: any) {
    const url = String(e.currentTarget.dataset.url || '')
    const id = e.currentTarget.dataset.id
    if (url) {
      wx.setClipboardData({ data: url, success: () => wx.showToast({ title: '网页链接已复制', icon: 'none' }) })
      return
    }
    if (id) wx.navigateTo({ url: `/pages/document/index?id=${id}` })
  },
  openDocument(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    if (id) wx.navigateTo({ url: `/pages/document/index?id=${id}` })
  },
  chooseModel() { if (!this.data.sending) this.setData({ modelPickerVisible: true }) },
  closeModelPicker() { if (!this.data.sending) this.setData({ modelPickerVisible: false }) },
  stopModelPickerBubble() { return },
  selectThinkingMode(e: any) {
    if (this.data.sending) return
    this.setData({ thinkingMode: e.currentTarget.dataset.mode as 'quick' | 'deep' })
  },
  selectModel(e: any) {
    if (this.data.sending) return
    const selected = this.data.modelOptions.find((item: any) => item.id === e.currentTarget.dataset.id)
    if (!selected) return
    this.setData({ selectedModelKey: selected.id, model: selected.value, modelShortLabel: selected.short, modelPickerVisible: false })
  },
})
