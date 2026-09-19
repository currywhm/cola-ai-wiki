import { addArtifact, appendTrace, assistantMessage, createFlusher, decorateSources, hydrateAssistant, markPlanReviewed, runningLabel, settleTrace, togglePlan } from '../../../utils/thread'
// 生成的文件：卡片打开 / 保存到微信都在 utils/artifact 里统一实现，三个会话页共用同一份
import { openArtifact as openArtifactFile, saveArtifact as saveArtifactFile } from '../../../utils/artifact'
import { ChatRunSnapshot, deleteConversation, followChatRun, getActiveChatRun, getConversation, getConversations, getKnowledge, getModels, pinConversation, Knowledge, Source, reviewPlan, stopChatRun, streamChat } from '../../../services/api'
import { KNOWLEDGE_PLACEHOLDER, PLANNER_PLACEHOLDER } from '../../utils/skills'
import { buildSharePayload, homePayload, questionFor } from '../../../utils/share'
// 多选分享：选中态、勾选映射、分享面板与「存到知识库」都在 utils/pick-page 里收口
import * as pickPage from '../../../utils/pick-page'
import { persistSkills, readLocalSkills, restoreSkills } from '../../../utils/skill-prefs'
import { readKeyboardHeight, repinLatest, shellStyle } from '../../../utils/keyboard'
import { markdownToText } from '../../../utils/markdown'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'

Page({
  data: {
    safeBottom: 0,
    navHeight: 88,
    keyboardHeight: 0,
    shellStyle: '',
    pickMode: false,
    pickedKeys: [] as string[],
    pickedMap: {} as any,
    pickCount: 0,
    pickTotal: 0,
    pickMessageCount: 0,
    pickFileCount: 0,
    shareSheetVisible: false,
    shareKnowledges: [] as any[],
    shareKnowledgeLoading: false,
    shareHintText: '',
    shareBusy: false,
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
    // 已启用的技能 id（可多选）。长期生效：只要用户不主动修改就一直沿用，
    // 发送后不移除、进入新对话也不重置；只在技能图标上以蓝色线条提示。
    selectedSkillIds: [] as string[],
    model: 'deepseek-flash',
    selectedModelKey: 'deepseek-flash',
  },
  onLoad() {
    this.setData({ selectedSkillIds: readLocalSkills(), skillsReady: false, skillsTouched: false })
    this.measureNav()
    this.measureSafeArea()
    this.loadModels()
    this.load()
    this.restoreSkillPrefs()
  },
  onShow() { this.measureSafeArea() },
  onUnload() {
    const cancel = (this as any).cancelStream
    if (cancel) cancel()
    const flusher = (this as any).flusher
    if (flusher) flusher.reset()
  },
  detachStream() {
    const cancel = (this as any).cancelStream
    if (cancel) cancel()
    ;(this as any).cancelStream = null
    ;(this as any).chatRunId = ''
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
    this.syncShell()
  },
  // 键盘避让：固定高度布局里 adjust-position 推不动页面，改为按键盘高度自己收缩外壳
  syncShell() {
    this.setData({ shellStyle: shellStyle(this.data.safeBottom, this.data.keyboardHeight) })
  },
  onKeyboardHeightChange(e: any) {
    const height = readKeyboardHeight(e)
    if (height === this.data.keyboardHeight) return
    this.setData({ keyboardHeight: height }, () => {
      this.syncShell()
      if (height > 0) repinLatest(this)
    })
  },
  // 收起键盘的兜底：个别机型只在弹起时给高度，失焦即恢复满屏
  onKeyboardBlur() {
    if (!this.data.keyboardHeight) return
    this.setData({ keyboardHeight: 0 }, () => this.syncShell())
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
    // 从「问AI」首页的历史对话进来时，带的是要恢复的会话与它所属的知识库
    const requestedConversation = String(wx.getStorageSync('qa_conversation_id') || '')
    const requestedKnowledge = String(wx.getStorageSync('qa_knowledge_id') || '')
    wx.removeStorageSync('qa_conversation_id')
    wx.removeStorageSync('qa_knowledge_id')
    getKnowledge().then((list) => {
      const items = list || []
      const current = (requestedKnowledge ? items.find((item) => item.id === requestedKnowledge) : undefined)
        || items.find((item) => item.name === DEFAULT_KNOWLEDGE_NAME) || items[0]
      this.setData({
        knowledgeList: items,
        filteredKnowledge: items,
        knowledgeId: current ? current.id : '',
        knowledgeName: current ? current.name : '',
        pendingKnowledgeId: current ? current.id : '',
        conversationId: requestedConversation,
        loadState: 'ready',
        readyForInput: true,
      }, () => {
        this.syncCanSend()
        if (requestedConversation) this.openConversation(requestedConversation)
        else this.consumeDraft()
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
  // 技能只随请求下发，不再往输入框里塞模板：改写输入不影响已选技能
  onInput(e: any) {
    this.setData({ input: e.detail.value, readyForInput: true }, () => this.syncCanSend())
  },
  clearSkill() {
    this.applySkills([])
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
  // 多选：点一下切换一个技能，面板不关闭；选择结果持久化，发送后与新建对话都不会重置。
  // 后端按 id 解析（内置技能走仓库内的技能包，我的技能走技能指令），没选就不注入。
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
    this.setData({ selectedSkillIds: ids, skillsTouched: true, readyForInput: true }, () => this.syncCanSend())
    persistSkills(ids)
  },
  restoreSkillPrefs() {
    restoreSkills().then((ids) => {
      const patch: any = { skillsReady: true }
      // 用户已经手动改过技能时，远端回包不能覆盖刚做的选择。
      if (!(this.data as any).skillsTouched) patch.selectedSkillIds = ids
      this.setData(patch)
    }).catch(() => this.setData({ skillsReady: true }))
  },
  // 新建 / 编辑技能：收起面板与跳转同一拍发出，不等面板收起的渲染回调
  onSkillCreate() { this.setData({ skillSheetVisible: false }); wx.navigateTo({ url: '/package-features/pages/skill-edit/index' }) },
  onSkillEdit(e: any) {
    const id = String((e.detail && e.detail.id) || '')
    if (!id) return
    this.setData({ skillSheetVisible: false }); wx.navigateTo({ url: `/package-features/pages/skill-edit/index?id=${encodeURIComponent(id)}` })
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
  // 问答页不预设开场白：进页时用户还没选「基于知识库问答 / 执行规划」，
  // 发一条招呼等于替用户把范围说死，所以这里保持空对话，等用户第一句话进来。
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
    if (this.data.pickMode) pickPage.exit(this)
    this.setData({ historyVisible: false, lastMessageId: '' })
    this.openConversation(id)
  },
  // 恢复一条历史会话：正文、思考过程、工具执行都由后端持久化，这里整条拉回来
  openConversation(id: string) {
    this.detachStream()
    this.setData({ conversationId: id, messages: [], sending: false, canSend: false }, () => this.syncCanSend())
    getConversation(id).then((messages) => {
      const hydrated = (messages || []).map((message: any) => message.role === 'assistant'
        ? hydrateAssistant(message)
        : message)
      this.setData({ messages: hydrated, lastMessageId: '' }, () => this.syncCanSend())
      this.resumeActiveRun(id)
    }).catch(() => wx.showToast({ title: '历史对话加载失败', icon: 'none' }))
  },
  newConversation() {
    this.detachStream()
    this.setData({ historyVisible: false, conversationId: '', messages: [], input: '', sending: false, canSend: false, readyForInput: true, lastMessageId: '' }, () => this.syncCanSend())
    if (this.data.pickMode) pickPage.exit(this)
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
          if (isCurrent) this.setData({ messages: [], lastMessageId: '' }, () => this.syncCanSend())
          wx.showToast({ title: '已删除', icon: 'success' })
        }).catch(() => wx.showToast({ title: '删除失败，请稍后重试', icon: 'none' }))
      },
    })
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
    if (!(this.data as any).skillsReady && !(this.data as any).skillsTouched) { wx.showToast({ title: '技能配置正在恢复，请稍后再试', icon: 'none' }); return }
    // 没有知识库时退回通用问答，保证任何状态下都能提问
    const mode: 'knowledge' | 'planner' = this.data.knowledgeId ? this.data.askMode : 'planner'
    this.doSend(mode, content)
  },
  doSend(askMode: 'knowledge' | 'planner', content: string) {
    const userId = `m${Date.now()}`
    const assistantId = `m${Date.now() + 1}`
    // 技能不再随发送清空：它是一段长期设定，只有用户在面板里改动才会变
    const skills = this.data.selectedSkillIds
    this.setData({
      input: '',
      sending: true,
      canSend: false,
      // 技能选中态保留在 selectedSkillIds 中，不在这里重置
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
    payload.skills = skills
    if (this.data.knowledgeId) payload.knowledge_id = this.data.knowledgeId
    ;(this as any).cancelStream = streamChat(
      payload,
      (meta) => { this.setData({ conversationId: meta.conversation_id, chatRunId: (meta.run && meta.run.id) || (this as any).chatRunId || '' }); this.updateAssistant(assistantId, assistant, meta.sources as Source[]) },
      // 增量文本按 70ms 节流合并提交，避免逐字 setData 引起的掉帧
      (delta) => { assistant += delta; this.flushDelta(assistantId, () => assistant) },
      () => {
        (this as any).cancelStream = null
        ;(this as any).chatRunId = ''
        this.flushReset(assistantId)
        this.updateAssistant(assistantId, assistant)
        this.settleAnswer(assistantId)
        this.setData({ sending: false })
        this.syncCanSend()
      },
      (error) => {
        (this as any).cancelStream = null
        ;(this as any).chatRunId = ''
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
      (run) => {
        assistant = run.answer || ''
        this.setData({ conversationId: run.conversation_id, chatRunId: run.id, sending: run.status === 'running', canSend: false })
        this.applyRunSnapshot(assistantId, run)
      },
    )
  },
  stopSend() {
    const runId = String((this as any).chatRunId || '')
    if (runId) stopChatRun(runId).catch(() => undefined)
    this.detachStream()
    const messages = this.data.messages
    const len = messages.length
    // 用户主动停止：移除本轮未完成的占位回答（连带前一条提问一起撤销，避免半截对话留在界面上）
    const dropLastPair = len >= 2 && messages[len - 1].role === 'assistant' && !messages[len - 1].content && messages[len - 2].role === 'user'
    const nextMessages = dropLastPair ? messages.slice(0, len - 2) : messages
    this.setData({ sending: false, messages: nextMessages, lastMessageId: nextMessages.length ? nextMessages[nextMessages.length - 1].id : '' }, () => this.syncCanSend())
  },
  applyRunSnapshot(id: string, run: ChatRunSnapshot, scroll = true) {
    // 快照每秒都会重建这条消息：跳过没人消费的 html（正文交给 <markdown-view>），长回答下省一大截
    const restored = hydrateAssistant({
      id,
      role: 'assistant',
      content: run.answer || '',
      sources: run.sources || [],
      trace: run.trace || [],
      reason: run.reason || '',
      artifacts: run.artifacts || [],
      duration_ms: run.duration_ms || 0,
    }, { skipHtml: true })
    const running = run.status === 'running'
    // 轮询快照没有 progress 帧：运行文案直接从过程区派生，别让用户一直看「正在思考…」
    const message = { ...restored, progress: running ? runningLabel(run.trace || []) : '', running, traceTitle: running ? (restored.trace && restored.trace.length ? '正在执行' : '思考中') : restored.traceTitle, traceElapsed: running ? '' : restored.traceElapsed }

    const messages = this.data.messages.map((item: any) => item.id === id ? message : item)
    const patch: any = { messages }
    if (scroll) patch.lastMessageId = id
    this.setData(patch)
  },
  resumeActiveRun(conversationId: string) {
    if (!conversationId) return
    getActiveChatRun(conversationId).then((run) => {
      if (!run || this.data.conversationId !== conversationId || run.status !== 'running') return
      const assistantId = `run-${run.id}`
      const existingIndex = this.data.messages.findIndex((item: any) => item.id === assistantId)
      let messages = this.data.messages.slice()
      if (existingIndex >= 0) {
        messages = messages.map((item: any) => item.id === assistantId ? assistantMessage(assistantId) : item)
      } else {
        const at = messages.findIndex((item: any) => item.id === run.user_message_id)
        if (at >= 0) messages.splice(at + 1, 0, assistantMessage(assistantId))
        else messages.push(assistantMessage(assistantId))
      }
      this.setData({ messages, lastMessageId: '', sending: true, canSend: false, chatRunId: run.id }, () => this.syncCanSend())
      this.applyRunSnapshot(assistantId, run, false)
      ;(this as any).cancelStream = followChatRun(run.id, {
        onSnapshot: (snapshot) => {
          if (this.data.conversationId !== conversationId) return
          this.setData({ chatRunId: snapshot.id, sending: snapshot.status === 'running' })
          this.applyRunSnapshot(assistantId, snapshot, false)
        },
        onDone: () => {
          ;(this as any).cancelStream = null
          ;(this as any).chatRunId = ''
          this.settleAnswer(assistantId)
          this.setData({ sending: false }, () => this.syncCanSend())
        },
        onError: (error) => {
          ;(this as any).cancelStream = null
          ;(this as any).chatRunId = ''
          this.settleAnswer(assistantId)
          this.setData({ sending: false }, () => this.syncCanSend())
          wx.showToast({ title: error.message || error.errMsg || '任务中断', icon: 'none' })
        },
      })
    }).catch(() => undefined)
  },
  updateAssistant(id: string, content: string, sources?: Source[]) {
    const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, content, progress: content ? '' : message.progress, sources: sources ? decorateSources(sources) : message.sources } : message)
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
  // 产物卡在多选态下改成勾选，不在多选态才走原来的打开逻辑
  onFileTap(e: any) {
    const id = String((e.currentTarget.dataset || {}).id || '')
    if (this.data.pickMode) { pickPage.toggle(this, pickPage.fileKey(id)); return }
    this.openArtifact(e)
  },
  // ---- 多选分享：长按消息进多选，选中内容与文件后分享到微信 / QQ / 钉钉 / 知识库 ----
  enterPick(e: any) { pickPage.enter(this, String((e.currentTarget.dataset || {}).key || '')) },
  togglePickItem(e: any) { pickPage.toggle(this, String((e.currentTarget.dataset || {}).key || '')) },
  exitPick() { pickPage.exit(this) },
  togglePickAll() { pickPage.toggleAll(this) },
  openShareSheet() { pickPage.openSheet(this) },
  armShareWechat() { pickPage.armWechat(this) },
  closeShareSheet() { pickPage.closeSheet(this) },
  shareCopy(e: any) { pickPage.copyFor(this, String((e.detail || {}).target || '')) },
  loadShareKnowledges() { pickPage.loadKnowledges(this) },
  saveShareToKnowledge(e: any) { const detail = e.detail || {}; pickPage.saveToKnowledge(this, String(detail.id || ''), String(detail.name || '')) },
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
  // 复制回答：只取本轮输出的纯文本（与渲染同一套分块，去掉 Markdown 标记），直接进剪贴板
  copyAnswer(e: any) {
    const id = String((e.currentTarget.dataset || {}).id || '')
    const message = (this.data.messages as any[]).find((item) => item && item.id === id)
    const raw = String((message && message.content) || '').trim()
    if (!raw) { wx.showToast({ title: '这条回答还没有内容', icon: 'none' }); return }
    wx.setClipboardData({
      data: markdownToText(raw) || raw,
      success: () => wx.showToast({ title: '回答已复制', icon: 'success' }),
      fail: () => wx.showToast({ title: '复制失败，请重试', icon: 'none' }),
    })
  },
  // 分享给微信好友：点「分享」直接拉起转发面板，卡片里带这条回答的分享页
  onShareAppMessage(e: any): any {
    // 多选分享：面板上点「微信好友」时把勾选的内容与文件打包成一张卡片；
    // 转发面板拉起的当下就收起分享面板与多选态，不留在屏幕上
    const prepared = pickPage.takeWechatPayload(this)
    if (prepared) {
      pickPage.exit(this)
      return prepared
    }
    if (this.data.pickMode) {
      const payload = pickPage.payloadOf(this)
      pickPage.exit(this)
      return payload
    }
    const id = String((e && e.target && e.target.dataset && e.target.dataset.id) || '')
    const messages = this.data.messages
    const index = messages.findIndex((message: any) => message.id === id)
    if (index < 0) return homePayload()
    return buildSharePayload({ message: messages[index], question: questionFor(messages, index), knowledgeName: this.data.knowledgeName })
  },
  // 参考出处默认收起：点标题行才展开文件列表，避免每条回答下面都拖一长串
  toggleSources(e: any) {
    const key = String(e.currentTarget.dataset.key || '')
    if (!key) return
    const messages = this.data.messages.map((message: any) => message.id === key ? { ...message, sourcesOpen: !message.sourcesOpen } : message)
    this.setData({ messages })
  },
  openSource(e: any) {
    const url = String(e.currentTarget.dataset.url || '')
    const id = e.currentTarget.dataset.id
    if (url) { wx.setClipboardData({ data: url, success: () => wx.showToast({ title: '网页链接已复制', icon: 'none' }) }); return }
    if (id) wx.navigateTo({ url: `/package-features/pages/document/index?id=${id}` })
  },
  retry() { this.load() },
})
