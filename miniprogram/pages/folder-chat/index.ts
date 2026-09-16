// 文件夹会话页：与「问AI」的独立问答页共用同一套结构、样式与过程区实现，
// 差别只有两点——顶部显示「文件夹 / 知识库」上下文，问答范围由后端锁定在该文件夹内。
import { renderMarkdown } from '../../utils/markdown'
import { addArtifact, appendTrace, assistantMessage, createFlusher, decorateSources, hydrateAssistant, markPlanReviewed, settleTrace, togglePlan } from '../../utils/thread'
// 生成的文件：卡片打开 / 保存到微信都在 utils/artifact 里统一实现，三个会话页共用同一份
import { openArtifact as openArtifactFile, saveArtifact as saveArtifactFile } from '../../utils/artifact'
import { FOLDER_PLACEHOLDER, PLANNER_PLACEHOLDER } from '../../utils/skills'
import { persistSkills, restoreSkills } from '../../utils/skill-prefs'
import { buildSharePayload, homePayload, questionFor } from '../../utils/share'
import { fileTypeLabel } from '../../utils/file-type'
import { deleteConversation, getConversation, getConversations, getKnowledgeDetail, getModels, getSuggestions, pinConversation, Source, reviewPlan, streamChat } from '../../services/api'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'

Page({
  data: {
    safeBottom: 0,
    navHeight: 88,
    loadState: 'loading' as 'loading' | 'ready' | 'error',
    knowledgeId: '',
    knowledgeName: '',
    folderId: '',
    folderName: '',
    documents: [] as any[],
    conversationId: '',
    messages: [] as any[],
    lastMessageId: '',
    suggestions: [] as string[],
    suggestionsLoading: false,
    input: '',
    placeholder: FOLDER_PLACEHOLDER,
    canSend: false,
    sending: false,
    readyForInput: false,
    autoFocus: false,
    askMode: 'knowledge' as 'knowledge' | 'planner',
    modelSheetVisible: false,
    skillSheetVisible: false,
    scopeSheetVisible: false,
    deepThinking: true,
    model: 'deepseek-flash',
    // 已启用的技能 id（可多选、长期生效，不在发送或切换页面时重置）
    selectedSkillIds: [] as string[],
    historyVisible: false,
    historyLoading: false,
    historyItems: [] as any[],
  },
  onLoad(options: any) {
    // query 参数可能经过 encodeURIComponent，必须解码，否则中文显示为 %XX 乱码
    const knowledgeId = String(options.knowledgeId || '')
    const knowledgeName = decodeURIComponent(String(options.knowledgeName || ''))
    const folderId = String(options.folderId || '')
    const folderName = decodeURIComponent(String(options.folderName || ''))
    this.setData({ knowledgeId, knowledgeName, folderId, folderName })
    this.measureNav()
    this.measureSafeArea()
    this.loadModels()
    this.loadFolder()
    this.restoreSkillPrefs()
  },
  onShow() { this.measureSafeArea() },
  onUnload() {
    const cancel = (this as any).cancelStream
    if (cancel) cancel()
    const flusher = (this as any).flusher
    if (flusher) flusher.reset()
  },
  // 与问答页一致：不留标题栏，但必须避让胶囊按钮
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
  // 真机 env(safe-area-inset-bottom) 偶发失效，用实测 Home 条高度兜底（取不到走 CSS）
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
      const current = options.find((item) => item.id === this.data.model) || options[0]
      this.setData({ model: current.value })
    }).catch(() => undefined)
  },
  loadFolder() {
    if (!this.data.knowledgeId || !this.data.folderId) {
      this.setData({ loadState: 'error', documents: [] })
      return
    }
    this.setData({ loadState: 'loading' })
    getKnowledgeDetail(this.data.knowledgeId).then((result: any) => {
      const documents = (((result && result.documents) || []) as any[])
        .filter((item: any) => String(item.folder_id || '') === this.data.folderId)
        .map((item: any) => ({ ...item, typeLabel: fileTypeLabel(item.file_type) }))
      this.setData({ documents, loadState: 'ready', readyForInput: true }, () => {
        this.seedGreeting()
        this.loadSuggestions()
        this.syncCanSend()
        this.restoreLatestConversation()
      })
    }).catch(() => this.setData({ documents: [], loadState: 'error', readyForInput: false, canSend: false }))
  },
  retry() { this.loadFolder() },
  // 本文件夹已有会话时直接续上，用户不用重新描述背景
  restoreLatestConversation() {
    if (this.data.conversationId) return
    getConversations({ knowledgeId: this.data.knowledgeId, folderId: this.data.folderId }).then((items) => {
      const latest = (items || [])[0]
      if (!latest || this.data.messages.some((message: any) => !message.local)) return
      this.setData({ conversationId: latest.id })
      this.loadConversation(latest.id)
    }).catch(() => undefined)
  },
  loadConversation(id: string) {
    getConversation(id).then((messages) => {
      const hydrated = (messages || []).map((message: any) => message.role === 'assistant'
        ? hydrateAssistant(message)
        : message)
      this.setData({ messages: hydrated, lastMessageId: hydrated.length ? hydrated[hydrated.length - 1].id : '' }, () => this.syncCanSend())
    }).catch(() => undefined)
  },
  // 开场白：本文件夹有多少资料先说清楚（本地消息，不写入历史）
  seedGreeting() {
    if (this.data.messages.some((message: any) => !message.local)) return
    const kbName = this.data.knowledgeName || DEFAULT_KNOWLEDGE_NAME
    const folderName = this.data.folderName || '当前文件夹'
    const count = this.data.documents.length
    const title = '你好，我是 cola'
    const desc = count
      ? `这里是「${kbName}」的「${folderName}」，共 ${count} 份资料，提问时我会只依据它们回答。`
      : `这里是「${kbName}」的「${folderName}」，目前还没有资料。先把文件移动进来，或直接提问也可以。`
    const greeting = { id: `greeting-${Date.now()}`, role: 'assistant', local: true, greeting: true, title, desc, sources: [], trace: [], reason: '', content: `${title}。${desc}` }
    this.setData({ messages: [greeting], lastMessageId: greeting.id })
  },
  // 提问建议：按「知识库 + 文件夹」范围生成；没资料或生成失败就不展示，不阻塞问答
  loadSuggestions() {
    if (!this.data.knowledgeId || !this.data.folderId) return
    this.setData({ suggestionsLoading: true })
    getSuggestions(this.data.knowledgeId, this.data.folderId).then((result: any) => {
      this.setData({ suggestions: (result && result.questions) || [], suggestionsLoading: false })
    }).catch(() => this.setData({ suggestions: [], suggestionsLoading: false }))
  },
  // 点提问建议 = 直接发出，不再回填输入框
  pickPrompt(e: any) {
    const question = String(e.currentTarget.dataset.text || '')
    if (!question || this.data.sending) return
    this.setData({ input: question }, () => this.send())
  },
  // 技能只随请求下发，不再往输入框里塞模板：改写输入不影响已选技能
  onInput(e: any) {
    this.setData({ input: e.detail.value, readyForInput: true }, () => this.syncCanSend())
  },
  clearSkill() {
    this.applySkills([])
  },
  syncCanSend() {
    this.setData({ canSend: !!this.data.readyForInput && !!this.data.input.trim() && !!this.data.knowledgeId && !!this.data.folderId && this.data.loadState === 'ready' && !this.data.sending })
  },
  toggleAskLogic() {
    if (this.data.sending) return
    const askMode = this.data.askMode === 'planner' ? 'knowledge' : 'planner'
    this.setData({ askMode, placeholder: askMode === 'planner' ? PLANNER_PLACEHOLDER : FOLDER_PLACEHOLDER, modelSheetVisible: false, skillSheetVisible: false, scopeSheetVisible: false }, () => this.syncCanSend())
  },
  openModelSheet() {
    if (this.data.sending) return
    this.setData({ modelSheetVisible: true, skillSheetVisible: false, scopeSheetVisible: false })
  },
  openSkillSheet() {
    if (this.data.sending) return
    this.setData({ skillSheetVisible: true, modelSheetVisible: false, scopeSheetVisible: false })
  },
  openScopeSheet() {
    if (this.data.sending) return
    this.setData({ scopeSheetVisible: true, modelSheetVisible: false, skillSheetVisible: false })
  },
  closeSheets() { this.setData({ modelSheetVisible: false, skillSheetVisible: false, scopeSheetVisible: false }) },
  noop() { return },
  toggleDeepThinking() { this.setData({ deepThinking: !this.data.deepThinking }) },
  // 多选：点一下切换一个技能，面板不关闭；隔离仍由后端按用户判定
  onSkillSelect(e: any) {
    const skill = ((e && e.detail && e.detail.skill) || {})
    if (!skill.id) return
    const ids = this.data.selectedSkillIds.slice()
    const index = ids.indexOf(skill.id)
    if (index === -1) ids.push(skill.id)
    else ids.splice(index, 1)
    this.applySkills(ids)
  },
  applySkills(ids: string[]) {
    this.setData({ selectedSkillIds: ids, readyForInput: true }, () => this.syncCanSend())
    persistSkills(ids)
  },
  restoreSkillPrefs() {
    restoreSkills().then((ids) => { if (ids.length) this.setData({ selectedSkillIds: ids }) }).catch(() => undefined)
  },
  // 新建 / 编辑技能：先收起面板，回来时重新打开就是最新列表
  onSkillCreate() { this.setData({ skillSheetVisible: false }, () => wx.navigateTo({ url: '/pages/skill-edit/index' })) },
  onSkillEdit(e: any) {
    const id = String((e.detail && e.detail.id) || '')
    if (!id) return
    this.setData({ skillSheetVisible: false }, () => wx.navigateTo({ url: `/pages/skill-edit/index?id=${encodeURIComponent(id)}` }))
  },
  // 计划模式（plan mode）：用户在计划卡片上的结论回写后端，后端转交运行时
  // 交回被阻塞的 exit_plan_mode；批准即开始执行，继续规划则由模型修订后再次呈交。
  onPlanReview(e: any) {
    const detail = e.detail || {}
    const messageId = String(detail.messageId || '')
    const reviewId = String(detail.reviewId || '')
    const approved = !!detail.approved
    if (!reviewId || !messageId) return
    this.setData({ messages: markPlanReviewed(this.data.messages, messageId, approved ? 'approved' : 'revising') })
    reviewPlan(reviewId, approved).then((result: any) => {
      if (!result || result.accepted === false) {
        this.setData({ messages: markPlanReviewed(this.data.messages, messageId, 'review') })
        wx.showToast({ title: '计划评审已超时，请重新提问', icon: 'none' })
        return
      }
      wx.showToast({ title: approved ? '已批准，开始执行' : '已提交，继续规划', icon: 'none' })
    }).catch(() => {
      this.setData({ messages: markPlanReviewed(this.data.messages, messageId, 'review') })
      wx.showToast({ title: '提交失败，请稍后重试', icon: 'none' })
    })
  },
  onPlanToggle(e: any) {
    const messageId = String((e.detail || {}).messageId || '')
    if (!messageId) return
    this.setData({ messages: togglePlan(this.data.messages, messageId) })
  },
  send() {
    const content = this.data.input.trim()
    if (!this.data.readyForInput || this.data.loadState !== 'ready' || !content || this.data.sending) return
    if (!this.data.knowledgeId || !this.data.folderId) { wx.showToast({ title: '文件夹信息缺失', icon: 'none' }); return }
    this.doSend(content)
  },
  doSend(content: string) {
    const userId = `m${Date.now()}`
    const assistantId = `m${Date.now() + 1}`
    // 技能是长期设定：发送后保留，只有用户主动修改才会变
    const skills = this.data.selectedSkillIds
    this.setData({
      input: '',
      sending: true,
      canSend: false,
      messages: [...this.data.messages, { id: userId, role: 'user', content, sources: [] }, assistantMessage(assistantId)],
      lastMessageId: assistantId,
    })
    let assistant = ''
    const payload: any = {
      mode: this.data.askMode === 'planner' ? 'web' : 'knowledge',
      conversation_id: this.data.conversationId || undefined,
      folder_id: this.data.folderId,
      knowledge_id: this.data.knowledgeId,
      content,
      model: this.data.model,
      thinking: this.data.deepThinking ? 'deep' : 'quick',
    }
    // 可以多选；后端逐个解析并注入
    if (skills.length) payload.skills = skills
    ;(this as any).cancelStream = streamChat(
      payload,
      (meta) => { this.setData({ conversationId: meta.conversation_id }); this.updateAssistant(assistantId, assistant, meta.sources as Source[]) },
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
      (artifact) => this.pushArtifact(assistantId, artifact),
    )
  },
  stopSend() {
    const cancel = (this as any).cancelStream
    if (cancel) { (this as any).cancelStream = null; cancel() }
    const messages = this.data.messages
    const len = messages.length
    // 用户主动停止：移除本轮未完成的占位回答（连带前一条提问一起撤销）
    const dropLastPair = len >= 2 && messages[len - 1].role === 'assistant' && !messages[len - 1].content && messages[len - 2].role === 'user'
    const nextMessages = dropLastPair ? messages.slice(0, len - 2) : messages
    this.setData({ sending: false, messages: nextMessages, lastMessageId: nextMessages.length ? nextMessages[nextMessages.length - 1].id : '' }, () => this.syncCanSend())
  },
  updateAssistant(id: string, content: string, sources?: Source[]) {
    const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, content, html: renderMarkdown(content), progress: content ? '' : message.progress, sources: sources ? decorateSources(sources) : message.sources } : message)
    this.setData({ messages, lastMessageId: id })
  },
  updateAssistantProgress(id: string, progress: string) {
    const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, progress } : message)
    this.setData({ messages })
  },
  // 过程区与增量节流统一走 utils/thread，与问AI页共用同一实现
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
  // 工具产物：agent 本轮生成的文件，后端收好之后随时推来，落在这一轮回答下面
  pushArtifact(id: string, artifact: any) {
    const result = addArtifact(this.data.messages, id, artifact)
    if (result.changed) this.setData({ messages: result.messages, lastMessageId: id })
  },
  // 文件卡只带 id：回到本轮消息里取完整产物（实时问答与历史回放走同一条）
  findArtifact(e: any) {
    const id = String((e.currentTarget.dataset || {}).id || '')
    if (!id) return null
    const messages = this.data.messages as any[]
    for (let index = 0; index < messages.length; index += 1) {
      const hit = (messages[index].artifacts || []).find((item: any) => item && item.id === id)
      if (hit) return hit
    }
    return null
  },
  openArtifact(e: any) {
    const item = this.findArtifact(e)
    if (item) openArtifactFile(item)
  },
  saveArtifact(e: any) {
    const item = this.findArtifact(e)
    if (item) saveArtifactFile(item)
  },
  toggleTrace(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    // 这一行折叠的是思维链全文；执行步骤行始终可见，不受它影响
    const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, reasonOpen: message.reasonOpen !== true } : message)
    this.setData({ messages })
  },
  // 历史对话：只看当前文件夹的会话，与其它文件夹、根目录天然隔离
  openHistory() {
    this.setData({ historyVisible: true, historyLoading: true })
    getConversations({ knowledgeId: this.data.knowledgeId, folderId: this.data.folderId }).then((items) => {
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
        ? hydrateAssistant(message)
        : message)
      this.setData({ messages: hydrated, lastMessageId: hydrated.length ? hydrated[hydrated.length - 1].id : '' }, () => this.syncCanSend())
    }).catch(() => { this.seedGreeting(); wx.showToast({ title: '历史对话加载失败', icon: 'none' }) })
  },
  newConversation() {
    this.setData({ historyVisible: false, conversationId: '', messages: [], input: '', sending: false, canSend: false, readyForInput: true, lastMessageId: '' }, () => {
      this.seedGreeting()
      if (!this.data.suggestions.length) this.loadSuggestions()
      this.syncCanSend()
    })
  },
  // 左滑出「删除」，二次确认后再删（服务端按用户名下校验）
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
          if (isCurrent) this.setData({ messages: [], lastMessageId: '' }, () => this.seedGreeting())
          wx.showToast({ title: '已删除', icon: 'success' })
        }).catch(() => wx.showToast({ title: '删除失败，请稍后重试', icon: 'none' }))
      },
    })
  },
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
  copyAnswer(e: any) {
    const content = String(e.currentTarget.dataset.content || '')
    if (!content) return
    wx.setClipboardData({ data: content, success: () => wx.showToast({ title: '回答已复制', icon: 'success' }) })
  },
  // 分享给微信好友：点「分享」直接拉起转发面板，卡片里带这条回答的分享页
  onShareAppMessage(e: any): any {
    const id = String((e && e.target && e.target.dataset && e.target.dataset.id) || '')
    const messages = this.data.messages
    const index = messages.findIndex((message: any) => message.id === id)
    if (index < 0) return homePayload()
    return buildSharePayload({ message: messages[index], question: questionFor(messages, index), knowledgeName: this.data.knowledgeName })
  },
  openSource(e: any) {
    const url = String(e.currentTarget.dataset.url || '')
    const id = e.currentTarget.dataset.id
    if (url) { wx.setClipboardData({ data: url, success: () => wx.showToast({ title: '网页链接已复制', icon: 'none' }) }); return }
    if (id) wx.navigateTo({ url: `/pages/document/index?id=${id}` })
  },
  openDocument(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    if (!id) return
    this.setData({ scopeSheetVisible: false })
    wx.navigateTo({ url: `/pages/document/index?id=${id}` })
  },
  backToDirectory() {
    this.setData({ scopeSheetVisible: false })
    wx.navigateBack({ delta: 1, fail: () => { wx.switchTab({ url: '/pages/chat/index' }) } })
  },
  goBack() {
    wx.navigateBack({ delta: 1, fail: () => { wx.switchTab({ url: '/pages/chat/index' }) } })
  },
})
