import { renderMarkdown } from '../../utils/markdown'
import { appendTrace, assistantMessage, createFlusher, settleTrace } from '../../utils/thread'
import { deleteConversation, getConversation, getConversations, getKnowledge, getModels, pinConversation, Knowledge, Source, streamChat } from '../../services/api'
import { KNOWLEDGE_PLACEHOLDER, PLANNER_PLACEHOLDER, SKILLS } from '../../utils/skills'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'

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
    historyVisible: false,
    historyLoading: false,
    historyItems: [] as any[],
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
    // 本轮选中的 harness 技能包（发送后清空，避免后续问题被旧技能影响）
    pendingSkill: '',
    pendingSkillName: '',
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
  onUnload() {
    const cancel = (this as any).cancelStream
    if (cancel) cancel()
    const flusher = (this as any).flusher
    if (flusher) flusher.reset()
  },
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
        this.seedGreeting()
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
    const input = e.detail.value
    // 用户改写输入时，若已经不包含技能模板，就取消本轮技能绑定
    const bound = this.data.pendingSkill ? SKILLS.find((item) => item.harness === this.data.pendingSkill) : undefined
    const stillBound = bound ? input.includes(bound.prompt) : false
    this.setData({ input, readyForInput: true, pendingSkill: stillBound ? this.data.pendingSkill : '', pendingSkillName: stillBound ? this.data.pendingSkillName : '' }, () => this.syncCanSend())
  },
  clearSkill() {
    const bound = this.data.pendingSkill ? SKILLS.find((item) => item.harness === this.data.pendingSkill) : undefined
    const input = bound ? this.data.input.replace(bound.prompt, '').trim() : this.data.input
    this.setData({ input, pendingSkill: '', pendingSkillName: '' }, () => this.syncCanSend())
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
    const base = this.data.input.trim()
    const input = base ? `${base}\n${skill.prompt}` : skill.prompt
    // 绑定后端技能包：发送时随请求下发，后端让 agent 先加载并注入该技能
    this.setData({ input, skillSheetVisible: false, readyForInput: true, pendingSkill: skill.harness, pendingSkillName: skill.name }, () => this.syncCanSend())
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
  // 冷启动进入问答页先给一句开场白：对话不再是一张空屏（本地消息，不写入历史）
  seedGreeting() {
    if (this.data.messages.length) return
    const name = this.data.knowledgeName || DEFAULT_KNOWLEDGE_NAME
    const greeting = {
      id: `greeting-${Date.now()}`,
      role: 'assistant',
      local: true,
      sources: [],
      trace: [],
      reason: '',
      content: `我是 cola，已在「${name}」待命。可以直接提问、把资料整理成知识条目，或让我写报告、做 PPT 大纲。`,
    }
    this.setData({ messages: [greeting], lastMessageId: greeting.id })
  },
  // 历史对话：拉取当前知识库根目录的会话列表（后端按 user_id + knowledge_id + folder_id 收窄）
  openHistory() {
    this.setData({ historyVisible: true, historyLoading: true })
    getConversations({ knowledgeId: this.data.knowledgeId, folderId: '' }).then((items) => {
      this.setData({ historyItems: items || [], historyLoading: false })
    }).catch(() => this.setData({ historyItems: [], historyLoading: false }))
  },
  closeHistory() { this.setData({ historyVisible: false }) },
  pickHistory(e: any) {
    const id = String((e.detail && e.detail.id) || '')
    if (!id) return
    this.setData({ historyVisible: false, conversationId: id, messages: [], sending: false, canSend: false }, () => this.syncCanSend())
    getConversation(id).then((messages) => {
      const hydrated = (messages || []).map((message: any) => message.role === 'assistant'
        ? { ...message, html: renderMarkdown(message.content || ''), trace: [], reason: '', traceTitle: '', traceOpen: false, running: false }
        : message)
      this.setData({ messages: hydrated, lastMessageId: hydrated.length ? hydrated[hydrated.length - 1].id : '' }, () => this.syncCanSend())
    }).catch(() => { this.seedGreeting(); wx.showToast({ title: '历史对话加载失败', icon: 'none' }) })
  },
  newConversation() {
    this.setData({ historyVisible: false, conversationId: '', messages: [], input: '', sending: false, canSend: false, readyForInput: true, lastMessageId: '' }, () => { this.seedGreeting(); this.syncCanSend() })
  },
  // 左滑出「删除」后二次确认再删。历史存在服务端，删除范围限定本用户。
  // 左滑「置顶 / 取消置顶」：只改当前用户自己的会话，置顶后排到列表最前。
  pinHistory(e: any) {
    const id = String((e.detail && e.detail.id) || '')
    const pinned = !!(e.detail && e.detail.pinned)
    if (!id) return
    pinConversation(id, pinned).then(() => {
      const items = this.data.historyItems.map((item: any) => item.id === id ? { ...item, pinned: pinned ? 1 : 0 } : item)
      this.setData({ historyItems: items })
      wx.showToast({ title: pinned ? '已置顶' : '已取消置顶', icon: 'none' })
    }).catch(() => wx.showToast({ title: '操作失败，请稍后重试', icon: 'none' }))
  },
  removeHistory(e: any) {
    const id = String((e.detail && e.detail.id) || '')
    if (!id) return
    wx.showModal({
      title: '删除这条对话',
      content: '对话内容与其中的问答记录会一起删除，且无法恢复。',
      confirmText: '删除',
      confirmColor: '#d94545',
      success: (res) => {
        if (!res.confirm) return
        deleteConversation(id).then(() => {
          const items = this.data.historyItems.filter((item: any) => item.id !== id)
          const isCurrent = this.data.conversationId === id
          this.setData({ historyItems: items, conversationId: isCurrent ? '' : this.data.conversationId })
          if (isCurrent) this.setData({ messages: [], lastMessageId: '' }, () => { this.seedGreeting(); this.syncCanSend() })
          wx.showToast({ title: '已删除', icon: 'success' })
        }).catch(() => wx.showToast({ title: '删除失败，请稍后重试', icon: 'none' }))
      },
    })
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
    const skill = this.data.pendingSkill
    this.setData({
      input: '',
      sending: true,
      canSend: false,
      pendingSkill: '',
      pendingSkillName: '',
      messages: [...this.data.messages, { id: userId, role: 'user', content, sources: [] }, assistantMessage(assistantId)],
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
    if (skill) payload.skill = skill
    if (this.data.knowledgeId) payload.knowledge_id = this.data.knowledgeId
    ;(this as any).cancelStream = streamChat(
      payload,
      (meta) => { this.setData({ conversationId: meta.conversation_id }); this.updateAssistant(assistantId, assistant, meta.sources as Source[]) },
      // 增量文本按 70ms 节流合并提交，避免逐字 setData 引起的掉帧
      (delta) => { assistant += delta; this.flushDelta(assistantId, () => assistant) },
      () => {
        (this as any).cancelStream = null
        this.flushReset(assistantId)
        this.updateAssistant(assistantId, assistant)
        this.settleAnswer(assistantId)
        this.setData({ sending: false })
        this.syncCanSend()
      },
      (error) => {
        (this as any).cancelStream = null
        this.flushReset(assistantId)
        this.updateAssistant(assistantId, assistant || '回答未完成，请稍后重新提问。')
        this.settleAnswer(assistantId)
        this.setData({ sending: false })
        this.syncCanSend()
        wx.showToast({ title: error.message || error.errMsg || '回答失败', icon: 'none' })
      },
      (label) => { if (label) this.updateAssistantProgress(assistantId, label) },
      (item) => this.pushTrace(assistantId, item),
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
  // 过程区与增量节流统一走 utils/thread：问答页、知识库会话、文件夹会话共用同一实现
  ensureFlusher() {
    if (!(this as any).flusher) {
      (this as any).flusher = createFlusher((id: string, content: string) => this.updateAssistant(id, content))
    }
    return (this as any).flusher
  },
  flushDelta(id: string, read: () => string) { this.ensureFlusher().schedule(id, read) },
  flushReset(id: string) { const flusher = (this as any).flusher; if (flusher) flusher.clear(id) },
  settleAnswer(id: string) { this.setData({ messages: settleTrace(this.data.messages, id) }) },
  pushTrace(id: string, item: any) {
    const result = appendTrace(this.data.messages, id, item)
    if (result.changed) this.setData({ messages: result.messages, lastMessageId: id })
  },
  toggleTrace(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, traceOpen: !message.traceOpen } : message)
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
