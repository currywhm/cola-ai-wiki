import { renderMarkdown } from '../../utils/markdown'
import { getKnowledge, getModels, Knowledge, Source, streamChat } from '../../services/api'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'
const PLANNER_PLACEHOLDER = '告诉我想做什么，我来规划执行——查询知识、生成PPT、撰写报告、整理知识库……'
const KNOWLEDGE_PLACEHOLDER = '基于全部知识，或@指定知识进行提问'

type SkillItem = { id: string; name: string; desc: string; icon: string; prompt: string }

// 技能落在真实动作上：选中后把任务写进输入框，仍由用户确认后发送
const SKILLS: SkillItem[] = [
  { id: 'organize', name: '整理知识库', desc: '把资料整理成结构化知识条目', icon: 'knowledge-pick', prompt: '把当前知识库里的资料整理成结构化的知识条目，按主题归类并列出要点。' },
  { id: 'report', name: '撰写报告', desc: '基于资料输出一份调研报告', icon: 'book', prompt: '基于当前知识库的资料写一份调研报告，包含背景、结论和关键数据。' },
  { id: 'deck', name: '生成 PPT', desc: '输出分页大纲与每页要点', icon: 'ppt', prompt: '基于当前知识库的资料输出一份 PPT 大纲，按页给出标题与要点。' },
  { id: 'diagram', name: '知识图解', desc: '把长文梳理成知识结构', icon: 'image', prompt: '把当前知识库里的长文整理成一份知识图解，按主题分组并给出层级关系。' },
]

Page({
  data: {
    safeBottom: 0,
    navHeight: 88,
    loadState: 'loading' as 'loading' | 'ready' | 'error',
    knowledgeId: '',
    knowledgeName: '',
    knowledgeList: [] as Knowledge[],
    filteredKnowledge: [] as Knowledge[],
    pendingKnowledgeId: '',
    knowledgeQuery: '',
    conversationId: '',
    messages: [] as any[],
    lastMessageId: '',
    input: '',
    placeholder: KNOWLEDGE_PLACEHOLDER,
    canSend: false,
    sending: false,
    readyForInput: false,
    autoFocus: false,
    // 两种问答逻辑：planner=执行规划（agent，含联网检索）；knowledge=基于知识库问答
    askMode: 'knowledge' as 'knowledge' | 'planner',
    modelSheetVisible: false,
    skillSheetVisible: false,
    knowledgeSheetVisible: false,
    deepThinking: true,
    model: 'deepseek-flash',
    selectedModelKey: 'deepseek-flash',
    skills: SKILLS,
  },
  onLoad() {
    this.measureNav()
    this.measureSafeArea()
    this.loadModels()
    this.load()
  },
  onShow() { this.measureSafeArea() },
  onUnload() { const cancel = (this as any).cancelStream; if (cancel) cancel() },
  // 顶部不留标题栏，但必须避让胶囊按钮，高度随机型变化
  measureNav() {
    try {
      const api = wx as any
      const info = api.getWindowInfo ? api.getWindowInfo() : wx.getSystemInfoSync()
      const rect = wx.getMenuButtonBoundingClientRect()
      const status = info.statusBarHeight || 20
      const valid = rect && rect.height > 0 && rect.top >= status
      const height = valid ? Math.max(44, (rect.top - status) * 2 + rect.height) : 44
      this.setData({ navHeight: status + height })
    } catch (e) { this.setData({ navHeight: 88 }) }
  },
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
      this.setData({ selectedModelKey: current.id, model: current.value })
    }).catch(() => undefined)
  },
  load() {
    this.setData({ loadState: 'loading' })
    getKnowledge().then((list) => {
      const items = list || []
      const current = items.find((item) => item.name === DEFAULT_KNOWLEDGE_NAME) || items[0]
      this.setData({
        knowledgeList: items,
        filteredKnowledge: items,
        knowledgeId: current ? current.id : '',
        knowledgeName: current ? current.name : '',
        pendingKnowledgeId: current ? current.id : '',
        loadState: 'ready',
        readyForInput: true,
      }, () => {
        this.syncCanSend()
        this.consumeDraft()
      })
    }).catch(() => this.setData({ loadState: 'error', readyForInput: false, canSend: false }))
  },
  // 问AI首页点输入框/推荐问题时把诉求带过来：带 autoSend 的等于用户已确认，直接发出
  consumeDraft() {
    const draft = String(wx.getStorageSync('qa_draft') || '')
    const autoSend = !!wx.getStorageSync('qa_auto_send')
    const mode = String(wx.getStorageSync('qa_mode') || 'knowledge')
    wx.removeStorageSync('qa_draft')
    wx.removeStorageSync('qa_auto_send')
    wx.removeStorageSync('qa_mode')
    if (!draft) return
    // 旧的「全网」入口统一落到执行规划，避免出现第三种逻辑
    const askMode = mode === 'web' ? 'planner' : 'knowledge'
    this.setData({ input: draft, askMode, placeholder: askMode === 'planner' ? PLANNER_PLACEHOLDER : KNOWLEDGE_PLACEHOLDER }, () => {
      if (autoSend) this.send()
      else this.syncCanSend()
    })
  },
  onInput(e: any) {
    this.setData({ input: e.detail.value, readyForInput: true })
    this.syncCanSend()
  },
  syncCanSend() {
    const scoped = this.data.askMode === 'planner' || !!this.data.knowledgeId
    this.setData({ canSend: !!this.data.readyForInput && !!this.data.input.trim() && scoped && this.data.loadState === 'ready' && !this.data.sending })
  },
  // 小机器人两态互切：执行规划 / 基于知识库问答
  toggleAskLogic() {
    if (this.data.sending) return
    const askMode = this.data.askMode === 'planner' ? 'knowledge' : 'planner'
    this.setData({
      askMode,
      placeholder: askMode === 'planner' ? PLANNER_PLACEHOLDER : KNOWLEDGE_PLACEHOLDER,
      modelSheetVisible: false,
      skillSheetVisible: false,
      knowledgeSheetVisible: false,
    }, () => this.syncCanSend())
  },
  openModelSheet() {
    if (this.data.sending) return
    this.setData({ modelSheetVisible: true, skillSheetVisible: false, knowledgeSheetVisible: false })
  },
  openSkillSheet() {
    if (this.data.sending) return
    this.setData({ skillSheetVisible: true, modelSheetVisible: false, knowledgeSheetVisible: false })
  },
  closeSheets() {
    this.setData({ modelSheetVisible: false, skillSheetVisible: false, knowledgeSheetVisible: false })
  },
  // 弹层内部点击不穿透到遮罩
  noop() { return },
  toggleDeepThinking() { this.setData({ deepThinking: !this.data.deepThinking }) },
  useSkill(e: any) {
    const skill = SKILLS.find((item) => item.id === e.currentTarget.dataset.id)
    if (!skill) return
    const input = this.data.input.trim() ? `${this.data.input.trim()}\n${skill.prompt}` : skill.prompt
    this.setData({ input, skillSheetVisible: false, readyForInput: true }, () => this.syncCanSend())
  },
  // 选择知识：与知识库页面同一份数据源
  openKnowledgeSheet() {
    if (this.data.sending) return
    this.setData({
      knowledgeSheetVisible: true,
      modelSheetVisible: false,
      skillSheetVisible: false,
      knowledgeQuery: '',
      pendingKnowledgeId: this.data.knowledgeId,
      filteredKnowledge: this.data.knowledgeList,
    })
    this.refreshKnowledge()
  },
  refreshKnowledge() {
    getKnowledge().then((list) => {
      const items = list || []
      const query = this.data.knowledgeQuery.trim().toLowerCase()
      this.setData({ knowledgeList: items, filteredKnowledge: query ? items.filter((item) => `${item.name}${item.description || ''}`.toLowerCase().includes(query)) : items })
    }).catch(() => undefined)
  },
  onKnowledgeSearch(e: any) {
    const query = String(e.detail.value || '').trim().toLowerCase()
    const items = this.data.knowledgeList
    this.setData({ knowledgeQuery: e.detail.value, filteredKnowledge: query ? items.filter((item) => `${item.name}${item.description || ''}`.toLowerCase().includes(query)) : items })
  },
  selectKnowledge(e: any) { this.setData({ pendingKnowledgeId: String(e.currentTarget.dataset.id || '') }) },
  confirmKnowledge() {
    const target = this.data.knowledgeList.find((item) => item.id === this.data.pendingKnowledgeId)
    if (!target) { this.setData({ knowledgeSheetVisible: false }); return }
    this.setData({ knowledgeId: target.id, knowledgeName: target.name, knowledgeSheetVisible: false, askMode: 'knowledge', placeholder: KNOWLEDGE_PLACEHOLDER }, () => this.syncCanSend())
  },
  openKnowledgeTab() {
    this.closeSheets()
    wx.switchTab({ url: '/pages/chat/index' })
  },
  goBack() {
    wx.navigateBack({ delta: 1, fail: () => { wx.switchTab({ url: '/pages/ask/index' }) } })
  },
  send() {
    const content = this.data.input.trim()
    if (!this.data.readyForInput || this.data.loadState !== 'ready' || !content || this.data.sending) return
    // 没有知识库时退回通用问答，保证任何状态下都能提问
    const mode: 'knowledge' | 'planner' = this.data.knowledgeId ? this.data.askMode : 'planner'
    this.doSend(mode, content)
  },
  doSend(askMode: 'knowledge' | 'planner', content: string) {
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
    const payload: any = {
      mode: askMode === 'planner' ? 'web' : 'knowledge',
      conversation_id: this.data.conversationId || undefined,
      content,
      model: this.data.model,
      thinking: this.data.deepThinking ? 'deep' : 'quick',
    }
    if (this.data.knowledgeId) payload.knowledge_id = this.data.knowledgeId
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
