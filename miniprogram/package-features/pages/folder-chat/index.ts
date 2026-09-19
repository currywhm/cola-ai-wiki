// 文件夹会话页：与「问AI」的独立问答页共用同一套结构、样式与过程区实现，
// 差别只有两点——顶部显示「文件夹 / 知识库」上下文，问答范围由后端锁定在该文件夹内。
import { addArtifact, appendTrace, assistantMessage, createFlusher, decorateSources, hydrateAssistant, markPlanReviewed, measureThreadBody, pinTail, pinTailSoon, resetTail, runningProgress, settleTrace, togglePlan, trackTail } from '../../../utils/thread'
// 生成的文件：卡片打开 / 保存到微信都在 utils/artifact 里统一实现，三个会话页共用同一份
import { openArtifact as openArtifactFile, saveArtifact as saveArtifactFile } from '../../../utils/artifact'
import { FOLDER_PLACEHOLDER, PLANNER_PLACEHOLDER } from '../../utils/skills'
import { persistSkills, readLocalSkills, restoreSkills } from '../../../utils/skill-prefs'
import { buildSharePayload, homePayload, questionFor } from '../../../utils/share'
// 多选分享：选中态、勾选映射、分享面板与「存到知识库」都在 utils/pick-page 里收口
import * as pickPage from '../../../utils/pick-page'
import { fileTypeLabel } from '../../../utils/file-type'
import { readKeyboardHeight, repinLatest, shellStyle } from '../../../utils/keyboard'
import { markdownToText } from '../../../utils/markdown'
import { ChatRunSnapshot, deleteConversation, followChatRun, getActiveChatRun, getConversation, getConversations, getKnowledgeDetail, getModels, getSuggestions, pinConversation, preheatHarness, Source, reviewPlan, stopChatRun, streamChat } from '../../../services/api'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'

Page({
  data: {
    safeBottom: 0,
    navHeight: 88,
    keyboardHeight: 0,
    shellStyle: '',
    scrollTop: 0,
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
    this.setData({ selectedSkillIds: readLocalSkills(), skillsReady: false, skillsTouched: false })
    this.measureNav()
    this.measureSafeArea()
    this.loadModels()
    this.loadFolder()
    this.restoreSkillPrefs()
  },
  onShow() { this.measureSafeArea(); preheatHarness().catch(() => undefined) },
  onReady() { this.measureBody() },
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
      this.measureBody()
      if (height > 0) repinLatest(this)
    })
  },
  // 收起键盘的兜底：个别机型只在弹起时给高度，失焦即恢复满屏
  onKeyboardBlur() {
    if (!this.data.keyboardHeight) return
    this.setData({ keyboardHeight: 0 }, () => this.syncShell())
  },
  // 滚动跟随：用户往上翻就暂停，回到底部自动恢复（见 utils/thread 的 pinTail）
  measureBody() { measureThreadBody(this, '.qa-body') },
  onThreadScroll(e: any) { trackTail(this, e) },
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
    this.detachStream()
    getConversation(id).then((messages) => {
      const hydrated = (messages || []).map((message: any) => message.role === 'assistant'
        ? hydrateAssistant(message)
        : message)
      this.setData({ messages: hydrated, lastMessageId: '' }, () => { this.syncCanSend(); resetTail(this); pinTailSoon(this) })
      this.resumeActiveRun(id)
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
    }, () => { resetTail(this); pinTail(this) })
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
    payload.skills = skills
    ;(this as any).cancelStream = streamChat(
      payload,
      (meta) => { this.setData({ conversationId: meta.conversation_id, chatRunId: (meta.run && meta.run.id) || (this as any).chatRunId || '' }); this.updateAssistant(assistantId, assistant, meta.sources as Source[]) },
      (delta) => { assistant += delta; this.flushDelta(assistantId, () => assistant) },
      () => {
        ;(this as any).chatRunId = ''
        ;(this as any).cancelStream = null
        this.flushReset(assistantId)
        this.updateAssistant(assistantId, assistant)
        this.settleAnswer(assistantId)
        this.setData({ sending: false })
        this.syncCanSend()
      },
      (error) => {
        ;(this as any).chatRunId = ''
        ;(this as any).cancelStream = null
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
    // 用户主动停止：移除本轮未完成的占位回答（连带前一条提问一起撤销）
    const dropLastPair = len >= 2 && messages[len - 1].role === 'assistant' && !messages[len - 1].content && messages[len - 2].role === 'user'
    const nextMessages = dropLastPair ? messages.slice(0, len - 2) : messages
    this.setData({ sending: false, messages: nextMessages, lastMessageId: nextMessages.length ? nextMessages[nextMessages.length - 1].id : '' }, () => { this.syncCanSend(); pinTail(this) })
  },
  applyRunSnapshot(id: string, run: ChatRunSnapshot, scroll = true) {
    // 快照每秒都会重建这条消息：跳过没人消费的 html（正文交给 <markdown-view>），长回答下省一大截
    const restored = hydrateAssistant({ id, role: 'assistant', content: run.answer || '', sources: run.sources || [], trace: run.trace || [], reason: run.reason || '', artifacts: run.artifacts || [], duration_ms: run.duration_ms || 0 }, { skipHtml: true })
    const running = run.status === 'running'
    // 轮询快照没有 progress 帧：运行文案直接从过程区派生，别让用户一直看「正在思考…」
    const message = { ...restored, progress: running ? runningProgress(run.trace || [], run.duration_ms || 0) : '', running, traceTitle: running ? (restored.trace && restored.trace.length ? '正在执行' : '思考中') : restored.traceTitle, traceElapsed: running ? '' : restored.traceElapsed }

    const patch: any = { messages: this.data.messages.map((item: any) => item.id === id ? message : item) }
    this.setData(patch)
    if (scroll) pinTail(this)
  },
  resumeActiveRun(conversationId: string) {
    if (!conversationId) return
    getActiveChatRun(conversationId).then((run) => {
      if (!run || this.data.conversationId !== conversationId || run.status !== 'running') return
      const assistantId = `run-${run.id}`
      let messages = this.data.messages.slice()
      const existing = messages.findIndex((item: any) => item.id === assistantId)
      if (existing >= 0) messages = messages.map((item: any) => item.id === assistantId ? assistantMessage(assistantId) : item)
      else {
        const at = messages.findIndex((item: any) => item.id === run.user_message_id)
        if (at >= 0) messages.splice(at + 1, 0, assistantMessage(assistantId))
        else messages.push(assistantMessage(assistantId))
      }
      this.setData({ messages, lastMessageId: '', sending: true, canSend: false, chatRunId: run.id }, () => { this.syncCanSend(); resetTail(this); pinTailSoon(this) })
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
    pinTail(this)
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
    if (result.changed) { this.setData({ messages: result.messages, lastMessageId: id }); pinTail(this) }
  },
  // 工具产物：agent 本轮生成的文件，后端收好之后随时推来，落在这一轮回答下面
  pushArtifact(id: string, artifact: any) {
    const result = addArtifact(this.data.messages, id, artifact)
    if (result.changed) { this.setData({ messages: result.messages, lastMessageId: id }); pinTail(this) }
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
    if (this.data.pickMode) pickPage.exit(this)
    this.setData({ historyVisible: false, conversationId: id, messages: [], sending: false, canSend: false, lastMessageId: '' }, () => { this.syncCanSend(); resetTail(this) })
    this.loadConversation(id)
  },
  newConversation() {
    this.detachStream()
    this.setData({ historyVisible: false, conversationId: '', messages: [], input: '', sending: false, canSend: false, readyForInput: true, lastMessageId: '' }, () => {
      if (this.data.pickMode) pickPage.exit(this)
      resetTail(this)
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
  openDocument(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    if (!id) return
    this.setData({ scopeSheetVisible: false })
    wx.navigateTo({ url: `/package-features/pages/document/index?id=${id}` })
  },
  backToDirectory() {
    this.setData({ scopeSheetVisible: false })
    wx.navigateBack({ delta: 1, fail: () => { wx.switchTab({ url: '/pages/chat/index' }) } })
  },
  goBack() {
    wx.navigateBack({ delta: 1, fail: () => { wx.switchTab({ url: '/pages/chat/index' }) } })
  },
})
