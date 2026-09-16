import { renderMarkdown } from '../../utils/markdown'
import { getConversations, getKnowledge, getModels, getSuggestions, ModelOption, Source, streamChat } from '../../services/api'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'
// 与后端 CHAT_MODELS 一致的兜底清单；正常运行时会被 /api/models 的返回值覆盖
const FALLBACK_MODEL_OPTIONS: ModelOption[] = [
  { id: 'deepseek-flash', name: '云枢', value: 'deepseek-flash', badge: '', short: '云枢' },
  { id: 'deepseek-v4-pro', name: '墨衡', value: 'deepseek-v4-pro', badge: '', short: '墨衡' },
]

Page({
  data: {
    safeBottom: 0,
    loadState: 'loading' as 'loading' | 'ready' | 'error',
    knowledgeId: '',
    knowledgeName: '',
    conversationId: '',
    messages: [] as any[],
    lastMessageId: '',
    input: '',
    canSend: false,
    sending: false,
    readyForInput: false,
    askMode: 'knowledge' as 'knowledge' | 'web',
    modePickerVisible: false,
    modelPickerVisible: false,
    model: 'deepseek-flash',
    selectedModelKey: 'deepseek-flash',
    modelShortLabel: '云枢',
    thinkingMode: 'quick' as 'quick' | 'deep',
    modelOptions: FALLBACK_MODEL_OPTIONS,
    suggestions: [] as string[],
  },
  onLoad() {
    this.measureSafeArea()
    this.loadModels()
    this.load()
  },
  onShow() { this.measureSafeArea() },
  onUnload() { const cancel = (this as any).cancelStream; if (cancel) cancel() },
  // 真机上 env(safe-area-inset-bottom) 偶发失效，这里用窗口信息实测 Home 条高度兜底
  measureSafeArea() {
    try {
      const info = (wx as any).getWindowInfo ? (wx as any).getWindowInfo() : wx.getSystemInfoSync()
      const bottom = info && info.safeArea ? Math.max(0, (info.windowHeight || 0) - (info.safeArea.bottom || 0)) : 0
      if (bottom !== this.data.safeBottom) this.setData({ safeBottom: bottom })
    } catch (e) { /* 忽略，继续使用 CSS env() 兜底 */ }
  },
  loadModels() {
    getModels().then((options) => {
      if (!options || !options.length) return
      const current = options.find((item) => item.id === this.data.selectedModelKey) || options[0]
      this.setData({ modelOptions: options, selectedModelKey: current.id, model: current.value, modelShortLabel: current.short })
    }).catch(() => undefined)
  },
  load() {
    this.setData({ loadState: 'loading' })
    getKnowledge().then((list) => {
      const current = list.find((item) => item.name === DEFAULT_KNOWLEDGE_NAME) || list[0]
      this.setData({
        knowledgeId: current ? current.id : '',
        knowledgeName: current ? current.name : '',
        loadState: 'ready',
        readyForInput: true,
      }, () => {
        this.syncCanSend()
        this.consumeDraft()
      })
      if (current) this.loadSuggestions(current.id)
    }).catch(() => this.setData({ loadState: 'error', readyForInput: false, canSend: false }))
  },
  // 问AI首页点输入框/推荐问题时把诉求带过来：带 autoSend 的等于用户已确认，直接发出
  consumeDraft() {
    const draft = String(wx.getStorageSync('qa_draft') || '')
    const autoSend = !!wx.getStorageSync('qa_auto_send')
    const mode = String(wx.getStorageSync('qa_mode') || 'knowledge') as 'knowledge' | 'web'
    wx.removeStorageSync('qa_draft')
    wx.removeStorageSync('qa_auto_send')
    wx.removeStorageSync('qa_mode')
    if (!draft) return
    if (autoSend) { this.setData({ input: draft, askMode: mode }, () => this.send()); return }
    this.setData({ input: draft, askMode: mode }, () => this.syncCanSend())
  },
  loadSuggestions(knowledgeId: string) {
    getSuggestions(knowledgeId).then((result: any) => this.setData({ suggestions: (result && result.questions) || [] })).catch(() => undefined)
  },
  onInput(e: any) {
    this.setData({ input: e.detail.value, readyForInput: true })
    this.syncCanSend()
  },
  syncCanSend() {
    this.setData({ canSend: !!this.data.readyForInput && !!this.data.input.trim() && (this.data.askMode === 'web' || !!this.data.knowledgeId) && this.data.loadState === 'ready' && !this.data.sending })
  },
  toggleModePicker() { if (!this.data.sending) this.setData({ modePickerVisible: !this.data.modePickerVisible }) },
  selectAskMode(e: any) {
    const mode = e.currentTarget.dataset.mode as 'knowledge' | 'web'
    if (mode === 'knowledge' && !this.data.knowledgeId) { wx.showToast({ title: '还没有可用知识库', icon: 'none' }); return }
    this.setData({ askMode: mode, modePickerVisible: false }, () => this.syncCanSend())
  },
  chooseModel() { if (!this.data.sending) this.setData({ modelPickerVisible: true, modePickerVisible: false }) },
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
  // 推荐问题是「一键提问」：直接发送，不再回填输入框
  useSuggestion(e: any) {
    if (this.data.sending) return
    const text = String(e.currentTarget.dataset.text || '')
    if (!text) return
    this.setData({ input: text }, () => this.send())
  },
  send() {
    const content = this.data.input.trim()
    if (!this.data.readyForInput || this.data.loadState !== 'ready' || !content || this.data.sending) return
    const mode = this.data.askMode
    if (mode === 'knowledge' && !this.data.knowledgeId) { wx.showToast({ title: '请先创建知识库', icon: 'none' }); return }
    this.doSend(mode, content)
  },
  doSend(mode: 'knowledge' | 'web', content: string) {
    const userId = `m${Date.now()}`
    const assistantId = `m${Date.now() + 1}`
    this.setData({
      input: '',
      sending: true,
      canSend: false,
      messages: [...this.data.messages, { id: userId, role: 'user', content, sources: [] }, { id: assistantId, role: 'assistant', content: '', html: '', progress: '正在思考…', sources: [] }],
      lastMessageId: assistantId,
    })
    let assistant = ''
    const payload: any = { mode, conversation_id: this.data.conversationId || undefined, content, model: this.data.model, thinking: this.data.thinkingMode }
    if (mode === 'knowledge') payload.knowledge_id = this.data.knowledgeId
    ;(this as any).cancelStream = streamChat(
      payload,
      (meta) => { this.setData({ conversationId: meta.conversation_id }); this.updateAssistant(assistantId, assistant, meta.sources as Source[]) },
      (delta) => { assistant += delta; this.updateAssistant(assistantId, assistant) },
      () => { (this as any).cancelStream = null; this.setData({ sending: false }); this.syncCanSend() },
      (error) => { (this as any).cancelStream = null; this.setData({ sending: false }); this.updateAssistant(assistantId, assistant || '回答未完成，请稍后重新提问。'); this.syncCanSend(); wx.showToast({ title: error.message || error.errMsg || '回答失败', icon: 'none' }) },
      (label) => { if (label) this.updateAssistantProgress(assistantId, label) },
    )
  },
  stopSend() {
    const cancel = (this as any).cancelStream
    if (cancel) { (this as any).cancelStream = null; cancel() }
    const messages = this.data.messages
    const len = messages.length
    // 用户主动停止：移除本轮未完成的占位回答（连带前一条提问一起撤销，避免半截对话留在界面上）
    const dropLastPair = len >= 2 && messages[len - 1].role === 'assistant' && !messages[len - 1].content && messages[len - 2].role === 'user'
    const nextMessages = dropLastPair ? messages.slice(0, len - 2) : messages
    this.setData({ sending: false, messages: nextMessages, lastMessageId: nextMessages.length ? nextMessages[nextMessages.length - 1].id : '' }, () => this.syncCanSend())
  },
  updateAssistant(id: string, content: string, sources?: Source[]) {
    const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, content, html: renderMarkdown(content), progress: content ? '' : message.progress, sources: sources || message.sources } : message)
    this.setData({ messages, lastMessageId: id })
  },
  updateAssistantProgress(id: string, progress: string) {
    const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, progress } : message)
    this.setData({ messages })
  },
  startNewConversation() {
    if (this.data.sending) return
    this.setData({ conversationId: '', messages: [], input: '', canSend: false, lastMessageId: '' }, () => this.syncCanSend())
  },
  openHistory() {
    getConversations().then((items) => {
      const visible = (items || []).filter((item: any) => !(item.folder_id || ''))
      if (!visible.length) { wx.showToast({ title: '暂无历史对话', icon: 'none' }); return }
      wx.showActionSheet({
        itemList: visible.slice(0, 6).map((item: any) => item.title || '未命名对话'),
        success: ({ tapIndex }) => {
          const item = visible[tapIndex]
          if (!item) return
          this.setData({ conversationId: item.id, knowledgeId: item.knowledge_id, messages: [], input: '', canSend: false })
        },
      })
    }).catch(() => wx.showToast({ title: '历史对话加载失败', icon: 'none' }))
  },
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
    if (url) { wx.setClipboardData({ data: url, success: () => wx.showToast({ title: '网页链接已复制', icon: 'none' }) }); return }
    if (id) wx.navigateTo({ url: `/pages/document/index?id=${id}` })
  },
  retry() { this.load() },
})
