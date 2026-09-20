import { consumeChatTarget } from '../../services/navigation'
import { addArtifact, appendTrace, assistantMessage, createFlusher, decorateSources, hydrateAssistant, markPlanReviewed, measureThreadBody, pinTail, pinTailSoon, resetTail, runningProgress, settleTrace, togglePlan, trackTail } from '../../utils/thread'
// 生成的文件：卡片打开 / 保存到微信都在 utils/artifact 里统一实现，三个会话页共用同一份
import { openArtifact as openArtifactFile, saveArtifact as saveArtifactFile } from '../../utils/artifact'
import { fileTypeKind, fileTypeLabel } from '../../utils/file-type'
import { ChatRunSnapshot, deleteConversation, followChatRun, getActiveChatRun, getConversation, getConversations, getKnowledge, getKnowledgeDetail, deleteDocument, deleteKnowledge, getModels, getFolders, getSuggestions, createFolder, deleteFolder, importArticle, moveDocument, pinConversation, preheatHarness, uploadLocalFile, Knowledge, ModelOption, Folder, Source, reviewPlan, stopChatRun, streamChat, uploadDocument, UploadSource } from '../../services/api'
import { warnPrivacyRequired } from '../../services/privacy'
import { buildSharePayload, homePayload, questionFor, SHARE_IMAGE } from '../../utils/share'
import { createKnowledgeShare } from '../../services/api'
import { goLogin, isLoggedIn } from '../../services/api'
import { contentAssetUrl } from '../../services/api'
import { applyLocalized, localizeImages } from '../../services/media'
// 多选分享：选中态、勾选映射、分享面板与「存到知识库」都在 utils/pick-page 里收口
import * as pickPage from '../../utils/pick-page'
import { persistSkills, readLocalSkills, restoreSkills } from '../../utils/skill-prefs'
import { dismissKeyboard, isGhostFocus, markOverlayClosed, readKeyboardHeight, repinLatest, shellStyle } from '../../utils/keyboard'
import { markdownToText } from '../../utils/markdown'
import { copyText } from '../../utils/clipboard'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'
// 与后端 CHAT_MODELS 一致的兜底清单；正常运行时会被 /api/models 的返回值覆盖
const FALLBACK_MODEL_OPTIONS: ModelOption[] = [
  { id: 'deepseek-flash', name: '云枢', value: 'deepseek-flash', badge: '', short: '云枢' },
  { id: 'deepseek-v4-pro', name: '墨衡', value: 'deepseek-v4-pro', badge: '', short: '墨衡' },
]
// 启动页已上移到小程序入口（问AI tab）：知识库页面不再拦截首屏，这里保留空标记保证旧调用安全
let bootSplashShown = true


Page({
  data: { safeBottom: 0, keyboardHeight: 0, shellStyle: '', scrollTop: 0, inputFocus: false, dirTouchStartX: 0, dirTouchStartY: 0, booting: !bootSplashShown, knowledgeId: '', knowledgeName: '', knowledgeDesc: '', conversationId: '', conversationActive: false, selectedIndex: 0, input: '', canSend: false, sending: false, readyForInput: false, lastMessageId: '', model: 'deepseek-flash', selectedModelKey: 'deepseek-flash', modelLabel: '云枢', modelShortLabel: '云枢', thinkingMode: 'quick' as 'quick' | 'deep', modelOptions: FALLBACK_MODEL_OPTIONS, modelPickerVisible: false, askMode: 'knowledge' as 'knowledge' | 'web', modePickerVisible: false, pinned: false, uploadSheetVisible: false, uploadUsedLabel: '0.00GB', uploadLimitLabel: '300MB', loadState:'loading', knowledge:[] as Knowledge[], filteredKnowledge:[] as Knowledge[], pickerQuery:'', pickerVisible:false, skillSheetVisible:false, historyVisible:false, historyLoading:false, historyItems:[] as any[], personalExpanded:true, subscriptionsExpanded:true, sharedExpanded:true, documents:[] as any[], documentsLoading:false, documentsError:false, folders:[] as Folder[], documentGroups:[] as any[], visibleDocuments:[] as any[], currentFolderId:'', currentFolderName:'', articleSheetVisible:false, articleUrl:'', articleImporting:false, importMode:false, pendingFileName:'', askLayerVisible:false, askFocus:false, askGreeting:'', suggestions:[] as string[], suggestionsFor:'', suggestionsLoading:false, messages: [] as any[], selectedSkillIds: [] as string[], pickMode:false, pickedKeys:[] as string[], pickedMap:{} as any, pickCount:0, pickTotal:0, pickMessageCount:0, pickFileCount:0, shareSheetVisible:false, shareKnowledges:[] as any[], shareKnowledgeLoading:false, shareHintText:'', shareBusy:false, personalKnowledge:[] as Knowledge[], subscriptionKnowledge:[] as Knowledge[], sharedKnowledge:[] as Knowledge[], filteredShared:[] as Knowledge[], filteredSubscriptions:[] as Knowledge[], knowledgeReadOnly:false, knowledgeShareable:true, knowledgeSourceMissing:false, kbShareVisible:false, kbShareArmed:false, kbSharePath:'', kbShareTitle:'', kbShareToken:'', kbShareDays:7 },
  onLoad() {
    // tabBar 为自定义组件：会话态、提问层、进入文件夹后的目录都要隐藏它，
    // 这里统一拦截 setData 同步，避免逐个调用点遗漏
    const originalSetData = this.setData.bind(this)
    // kbShareVisible：邀请面板是底部弹层，底部 tabBar 压在它上面会吃「选择微信好友」按钮的点击与视线
    // 弹层与全屏视图都要盖住 tabBar：任何底部面板（选择知识库 / 模型 / 技能 / 分享 / 历史）
    // 只要还在，底部 bar 都会压住它的下缘（选择知识库面板是 90vh，搜索框下面的一截会被挡）。
    const tabBarKeys = ['conversationActive', 'askLayerVisible', 'currentFolderId', 'kbShareVisible', 'uploadSheetVisible', 'articleSheetVisible', 'pickerVisible', 'modelPickerVisible', 'skillSheetVisible', 'shareSheetVisible', 'historyVisible', 'modePickerVisible']
    // shellKeys：外壳高度与底部留白随会话态、键盘高度变化，统一在 setData 回调里重算，避免逐个调用点遗漏
    const shellKeys = ['conversationActive', 'currentFolderId', 'safeBottom', 'keyboardHeight']
    ;(this as any).setData = (data: any, callback?: () => void) => {
      originalSetData(data, () => {
        if (data && tabBarKeys.some((key) => Object.prototype.hasOwnProperty.call(data, key))) this.syncTabBar()
        if (data && shellKeys.some((key) => Object.prototype.hasOwnProperty.call(data, key))) this.syncShell()
        // 会话里只剩下开场白时，顺手拉一次推荐问题（后端按指纹缓存）
        if (data && Array.isArray(data.messages) && data.messages.length === 1 && data.messages[0] && data.messages[0].greeting) this.ensureSuggestions()
        if (callback) callback()
      })
    }
    this.primeThreadState()
    this.syncShell()
  },
  // 自定义 tabBar 需要页面自己同步选中项与显隐：会话视图为全屏，隐藏底部导航
  // 会话视图与问AI问答页共用一套交互：技能包、历史对话抽屉、顶部安全高度
  primeThreadState() {
    // selectedSkillIds 在 restoreSkillPrefs 之前先给出空数组，避免异步返回前的空值访问
    // 本地选择同步恢复，不能等远端偏好返回，否则用户立刻发送时会丢掉技能。
    this.setData({ navHeight: 88, skillSheetVisible: false, selectedSkillIds: readLocalSkills(), historyVisible: false, historyLoading: false, historyItems: [] })
    // 知识库的三种状态：好友共享过来的库只读；「邀请好友」面板的态也在这里先落好
    this.setData({ personalKnowledge: [] as Knowledge[], subscriptionKnowledge: [] as Knowledge[], sharedKnowledge: [] as Knowledge[], filteredSubscriptions: [] as Knowledge[], filteredShared: [] as Knowledge[], knowledgeReadOnly: false, knowledgeShareable: false, knowledgeSourceMissing: false, kbShareVisible: false, kbShareArmed: false, kbSharePath: '', kbShareTitle: '', kbShareToken: '', kbShareDays: 7 })
    this.setData({ skillsReady: false, skillsTouched: false })
    this.restoreSkillPrefs()
    this.measureNav()
  },
  syncTabBar() {
    if (typeof this.getTabBar !== 'function') return
    const bar = this.getTabBar() as any
    if (!bar || typeof bar.setData !== 'function') return
    // 只有四个 tab 首页保留底部导航：会话页、提问层、以及「进入文件夹后的目录」都是从首页钻进去的视图
    // 上拉型弹层（导入文件 / 邀请好友）会被原生 tabBar 盖住底部按钮，所以打开时也收起
    bar.setData({ selected: 1, hidden: !!(this.data.conversationActive || this.data.askLayerVisible || this.data.currentFolderId || this.data.kbShareVisible || this.data.uploadSheetVisible || this.data.articleSheetVisible || this.data.pickerVisible || this.data.modelPickerVisible || this.data.skillSheetVisible || this.data.shareSheetVisible || this.data.historyVisible || this.data.modePickerVisible) })
  },
  onShow() {
    this.measureNav()
    this.syncTabBar()
    this.measureSafeArea()
    this.measureBody()
    // 提前把后端运行时拉起来（官方 SDK 冷启动 0.7–3.8s），失败静默
    preheatHarness().catch(() => undefined)
    const target = consumeChatTarget()
    if (!isLoggedIn()) {
      this.setData({
        loadState: 'guest',
        knowledge: [] as Knowledge[],
        personalKnowledge: [] as Knowledge[],
        subscriptionKnowledge: [] as Knowledge[],
        sharedKnowledge: [] as Knowledge[],
        filteredKnowledge: [] as Knowledge[],
        filteredSubscriptions: [] as Knowledge[],
        filteredShared: [] as Knowledge[],
        knowledgeId: '',
        knowledgeName: '',
        knowledgeDesc: '',
        documents: [],
        folders: [],
        documentGroups: [],
        visibleDocuments: [],
        messages: [],
        conversationActive: false,
        readyForInput: false,
        canSend: false,
      })
      if (target) goLogin()
      return
    }
    if (target && target.folderId) {
      // 从「最近」点文件夹：不进会话，直接落在该文件夹的目录视图
      // （目录加载完成后由 loadDirectory 里的 pendingFolderId 消费）
      ;(this as any).pendingFolderId = String(target.folderId)
      this.setData({
        knowledgeId: target.knowledgeId,
        conversationId: '',
        conversationActive: false,
        currentFolderId: '',
        currentFolderName: '',
        messages: [],
        input: '',
        canSend: false,
        readyForInput: false,
        lastMessageId: '',
      })
      this.load(false)
      return
    }
    if (target) this.setData({ knowledgeId:target.knowledgeId, conversationId:target.conversationId || '', conversationActive:true, messages:[], input:'', canSend:false, readyForInput:false, lastMessageId:'' })
    this.load(!!target)
  },
  // 会话态顶部不排标题栏，按胶囊按钮位置实测高度，和问AI问答页保持同一套版式
  measureNav() {
    try {
      const api = wx as any
      const info = api.getWindowInfo ? api.getWindowInfo() : wx.getSystemInfoSync()
      const rect = wx.getMenuButtonBoundingClientRect()
      const status = info.statusBarHeight || 20
      const valid = rect && rect.height > 0 && rect.top >= status
      const height = valid ? Math.max(44, (rect.top - status) * 2 + rect.height) : 44
      const next = status + height
      if (next !== (this.data as any).navHeight) this.setData({ navHeight: next })
    } catch (e) { /* 取不到就沿用默认高度 */ }
  },
  // 取到值时以内联样式覆盖 CSS 的 env()，取不到时继续走 CSS，不会重复叠加
  // 真机上 env(safe-area-inset-bottom) 偶发失效，用窗口信息实测 Home 条高度兜底；
  measureSafeArea() {
    try {
      const info = (wx as any).getWindowInfo ? (wx as any).getWindowInfo() : wx.getSystemInfoSync()
      const bottom = info && info.safeArea ? Math.max(0, (info.windowHeight || 0) - (info.safeArea.bottom || 0)) : 0
      if (bottom !== this.data.safeBottom) this.setData({ safeBottom: bottom })
    } catch (e) { /* 忽略，继续使用 CSS env() 兜底 */ }
    this.syncShell()
  },
  // 键盘避让 + 底部留白：会话态不留呼吸位，目录态给 tabBar 上方留出空间；键盘弹起时统一清零
  syncShell() {
    const conversation = this.data.conversationActive
    const folder = !conversation && !!this.data.currentFolderId
    // 底部导航高 104rpx，这里留 128rpx + 实测安全区（bar 之上还有 24rpx 呼吸位）。
    // 不能只靠 CSS 的 env()：真机上 env(safe-area-inset-bottom) 偶发为 0，而 tabBar 用的是 JS 实测值，
    // 两者一差就把输入框压到 bar 底下。
    const base = conversation ? 0 : (folder ? 16 : 128)
    const padding = `calc(${base}rpx + ${this.data.safeBottom}px)`
    this.setData({ shellStyle: shellStyle(this.data.safeBottom, this.data.keyboardHeight, padding) })
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
  // 输入框的焦点由数据驱动：只在真正变化时才生效——单调用 wx.hideKeyboard() 收不掉键盘，
  // 输入框仍持有焦点的话它会被重新拉起。另外兜住"点击穿透"：弹层刚关闭时的那次聚焦要撤销。
  onInputFocus() {
    if (isGhostFocus(this)) { this.setData({ inputFocus: false }); dismissKeyboard(); return }
    if (!this.data.inputFocus) this.setData({ inputFocus: true })
  },
  onAskFocus() {
    if (isGhostFocus(this)) { this.setData({ askFocus: false }); dismissKeyboard(); return }
    if (!this.data.askFocus) this.setData({ askFocus: true })
  },
  dismissInput() { this.setData({ inputFocus: false, askFocus: false }); dismissKeyboard() },
  // 滚动跟随：用户往上翻就暂停，回到底部自动恢复（见 utils/thread 的 pinTail）
  measureBody() { measureThreadBody(this, '.messages') },
  onThreadScroll(e: any) { trackTail(this, e) },
  finishBoot() {
    // 启动页已上移到小程序入口（问AI tab）：知识库页不再有首屏加载动画
  },
  load(loadConversation = false) {
    if (!isLoggedIn()) {
      this.setData({ loadState: 'guest', readyForInput: false, canSend: false })
      return
    }
    this.setData({ loadState:'loading', readyForInput:false, canSend:false })
    this.loadModels()
    getKnowledge().then(async (knowledgeRaw) => {
      const knowledge = knowledgeRaw.map((item: any) => ({ ...item, avatarUrl: item.avatar ? contentAssetUrl(item.avatar) : '' }))
      // 头像存在对象存储里、接口下发的是相对路径：先取回本地文件再交给 <image>
      const avatars = await localizeImages(knowledge.map((item: any) => item.avatarUrl).filter(Boolean))
      knowledge.forEach((item: any) => { if (item.avatarUrl) item.avatarUrl = applyLocalized(item.avatarUrl, avatars) })
      // 刚从创建页 / 分享落地页回来时，默认选中那一个知识库（一次性标记，读完即清）
      const focusId = String(wx.getStorageSync('kb_focus') || '')
      if (focusId) wx.removeStorageSync('kb_focus')
      const targetId = focusId || this.data.knowledgeId
      const currentIndex = knowledge.findIndex(k=>k.id===targetId)
      const defaultIndex = knowledge.findIndex(k=>k.name === DEFAULT_KNOWLEDGE_NAME)
      const selectedIndex = currentIndex >= 0 ? currentIndex : (defaultIndex >= 0 ? defaultIndex : 0)
      const current=knowledge[selectedIndex]
      // 广场订阅、好友共享和「共享知识库」收件箱各自成组，避免同一种只读镜像混在一起
      const personalKnowledge = knowledge.filter((item) => !item.shared && !item.subscribed && !item.inbox).map((item: any) => ({ ...item, metaLabel: `${item.document_count || 0} 份文档` }))
      const subscriptionKnowledge = knowledge.filter((item) => item.subscribed).map((item: any) => ({ ...item, metaLabel: `已订阅 · ${item.document_count || 0} 份资料${item.source_missing ? ' · 来源已删除' : ''}`, badgeLabel: item.source_missing ? '已失效' : '订阅' }))
      const sharedKnowledge = knowledge.filter((item) => !item.subscribed && (item.shared || item.inbox)).map((item: any) => ({
        ...item,
        metaLabel: item.inbox
          ? `${item.document_count || 0} 份内容 · 好友分享给你的`
          : `好友共享 · ${item.document_count || 0} 份资料${item.source_missing ? ' · 来源已删除' : ''}`,
        // 收件箱本身可写（别人分享来的内容就落在里面），所以不挂「只读」标签
        badgeLabel: item.inbox ? '' : (item.source_missing ? '已失效' : '只读'),
      }))
      this.setData({ knowledge, personalKnowledge, subscriptionKnowledge, sharedKnowledge, filteredKnowledge:personalKnowledge, filteredSubscriptions:subscriptionKnowledge, filteredShared:sharedKnowledge, selectedIndex, loadState:'ready', knowledgeId:current ? current.id : '', knowledgeName:current ? current.name : '', knowledgeDesc:current ? (current.description || '') : '', knowledgeReadOnly:!!(current && current.read_only), knowledgeShareable:!!(current && current.shareable), knowledgeSourceMissing:!!(current && current.source_missing) }, () => {
        // Only an explicit user action (typing or choosing a suggestion) may make
        // the composer sendable. Page entry must never create a chat request.
        this.setData({ readyForInput:true }, () => this.syncCanSend())
        this.finishBoot()
      })
      if (current) this.loadDirectory(current.id)
      else this.setData({ documents:[], documentsLoading:false, documentsError:false })
      if (current && this.data.conversationId && loadConversation) this.loadConversation()
      else if (current && loadConversation && this.data.conversationActive && !this.data.messages.length) {
        const guide = this.buildGuideMessage(this.data.askMode)
        this.setData({ messages:[guide], lastMessageId:guide.id, readyForInput:true }, () => this.syncCanSend())
      }
      this.maybePromptOpenFileImport()
      // 从问AI页跳转过来：自动打开提问层，可带上问AI页选中的提问内容
      const shouldOpenAsk = wx.getStorageSync('open_ask_layer')
      if (shouldOpenAsk) {
        const draft = String(wx.getStorageSync('ask_draft') || '')
        wx.removeStorageSync('open_ask_layer')
        wx.removeStorageSync('ask_draft')
        setTimeout(() => this.openAskLayer(draft), 300)
      }
    }).catch((error: any) => this.setData({ loadState: error?.loggedOut ? 'loggedout' : 'error', readyForInput:false, canSend:false }, () => this.finishBoot()))
  },
  // 微信「用小程序打开」进入：弹出知识库选择，选完直接把文件上传到目标知识库
  maybePromptOpenFileImport() {
    const app = getApp<IAppOption>()
    const file = app.globalData.pendingImportFile
    if (!file) return
    if (!this.data.knowledge.length) {
      app.globalData.pendingImportFile = null
      wx.showToast({ title: '请先创建知识库，再导入文件', icon: 'none' })
      return
    }
    this.setData({ importMode: true, pendingFileName: file.filename, pickerVisible: true, pickerQuery: '', filteredKnowledge: this.data.personalKnowledge, filteredSubscriptions: [], filteredShared: [] })
  },
  uploadPendingFile(knowledgeId: string) {
    const app = getApp<IAppOption>()
    const file = app.globalData.pendingImportFile
    if (!file) return
    getFolders(knowledgeId).catch(() => [] as Folder[]).then((folders) => {
      this.pickTargetFolder(folders as Folder[], (folderId) => {
        app.globalData.pendingImportFile = null
        uploadLocalFile(knowledgeId, file.path, file.filename, folderId).then((result) => {
          wx.showToast({ title: result.status === 'completed' ? '已导入并完成解析' : '已导入，正在解析', icon: 'none' })
          this.load()
        }).catch((error: any) => {
          wx.showToast({ title: error?.message || '导入失败，请重试', icon: 'none', duration: 2500 })
        })
      })
    })
  },
  loadDirectory(knowledgeId: string) {
    this.setData({ documentsLoading:true, documentsError:false })
    Promise.all([getKnowledgeDetail(knowledgeId), getFolders(knowledgeId).catch(() => [] as Folder[])]).then((pair) => {
      // 不用数组解构赋值：开发者工具的 babel 运行时会为解构注入 slicedToArray helper，
      // 该 helper 在部分基础库组合下注入失败会导致整页模块加载中断（页面白屏）
      const result = pair[0]
      const folders = pair[1]
      const documents = ((result as any).documents || []).map((item: any) => ({
        ...item,
        typeKind: fileTypeKind(item.file_type),
        typeLabel: fileTypeLabel(item.file_type),
        displaySize: this.formatSize(item.file_size),
        displayTime: this.displayTime(item.created_at || item.updated_at),
        swipeX: 0,
      }))
      const usedBytes = documents.reduce((sum: number, item: any) => sum + Number(item.file_size || 0), 0)
      // 切换知识库时清空文件夹视图；文件夹被删除时自动退回根目录；看文档返回时保持当前位置
      const folderList = folders as Folder[]
      const folderStillThere = folderList.some((f) => f.id === this.data.currentFolderId)
      const currentFolderId = knowledgeId === this.data.knowledgeId && folderStillThere ? this.data.currentFolderId : ''
      const currentFolderName = currentFolderId ? (folderList.find((f) => f.id === currentFolderId) || { name: '' }).name : ''
      const documentGroups = this.buildDocumentGroups(documents, folderList, currentFolderId)
      this.setData({
        documents,
        folders: folderList,
        currentFolderId,
        currentFolderName,
        documentGroups,
        visibleDocuments: this.visibleDocumentsOf(documentGroups),
        documentsLoading:false,
        documentsError:false,
        uploadUsedLabel: this.formatStorage(usedBytes),
      }, () => {
        this.syncCanSend()
        // 「最近」带来的文件夹目标：目录就绪后再展开，避免用还没加载的文件夹列表
        const wanted = String((this as any).pendingFolderId || '')
        if (wanted) {
          ;(this as any).pendingFolderId = ''
          if (folderList.some((f) => f.id === wanted)) this.enterFolderById(wanted)
        }
      })
    }).catch(() => this.setData({ documents:[], documentGroups:[], documentsLoading:false, documentsError:true }))
  },
  // 置顶为本地偏好（后端暂无字段），按知识库持久化到本地存储
  loadPinState(): { docs: Record<string, string[]>, folders: Record<string, string[]> } {
    try { return wx.getStorageSync('llmwiki_dir_pins') || { docs: {}, folders: {} } } catch (e) { return { docs: {}, folders: {} } }
  },
  savePinState(state: { docs: Record<string, string[]>, folders: Record<string, string[]> }) {
    try { wx.setStorageSync('llmwiki_dir_pins', state) } catch (e) { /* 存储失败忽略 */ }
  },
  buildDocumentGroups(this: any, documents: any[], folders: Folder[], currentFolderId = this.data.currentFolderId) {
    const pins = this.loadPinState()
    const kid = this.data.knowledgeId
    const pinFirst = <T>(list: T[], idOf: (item: T) => string, pinned: string[]) => [
      ...list.filter((item) => pinned.includes(idOf(item))),
      ...list.filter((item) => !pinned.includes(idOf(item))),
    ]
    const pinnedDocs = (pins.docs[kid] || []) as string[]
    const pinnedFolders = (pins.folders[kid] || []) as string[]
    const docView = (list: any[]) => pinFirst(list, (d) => String(d.id), pinnedDocs)
      .map((d) => ({ ...d, swipeX: 0, pinned: pinnedDocs.includes(String(d.id)) }))
    // 已进入文件夹：只保留该文件夹一个分组（文档列表由 visibleDocumentsOf 取出）
    if (currentFolderId) {
      const folder = folders.find((f) => f.id === currentFolderId)
      return [{ key: currentFolderId, name: folder ? folder.name : this.data.currentFolderName, documents: docView(documents.filter((d) => d.folder_id === currentFolderId)), swipeX: 0, pinned: false }]
    }
    const groups: any[] = []
    const rootDocs = docView(documents.filter((d) => !d.folder_id || !folders.some((f) => f.id === d.folder_id)))
    if (rootDocs.length || !folders.length) groups.push({ key: '', name: '', documents: rootDocs, swipeX: 0, pinned: false })
    pinFirst(folders, (f) => f.id, pinnedFolders).forEach((folder) => {
      groups.push({ key: folder.id, name: folder.name, documents: docView(documents.filter((d) => d.folder_id === folder.id)), swipeX: 0, pinned: pinnedFolders.includes(folder.id) })
    })
    return groups
  },
  // 当前视图下应展示的文档：根目录=未入夹文档；文件夹内=该夹文档
  // folderId 显式传入：切换文件夹的那一次 setData 之前 data 里还是旧值，不能读 data
  visibleDocumentsOf(this: any, groups: any[], folderId = this.data.currentFolderId) {
    if (folderId) return groups.length ? groups[0].documents : []
    const root = groups.find((g) => !g.key)
    return root ? root.documents : []
  },
  refreshDirectory() {
    const documentGroups = this.buildDocumentGroups(this.data.documents, this.data.folders)
    this.setData({ documentGroups, visibleDocuments: this.visibleDocumentsOf(documentGroups) })
  },
  // 点文件夹＝进入这个文件夹（在知识库里就地展开它的文件列表），
  // 问答留在下一步：由目录底部的输入框带着文件夹身份进独立的文件夹问答页。
  enterFolder(e: any) {
    const key = String(e.currentTarget.dataset.key || '')
    if (!key) return
    // 行已左滑展开时，第一次点击先收起操作按钮
    if (this.getDirSwipe('folder', key) < 0) { this.applyDirSwipe('folder', key, 0); return }
    const folder = this.data.folders.find((f) => f.id === key)
    if (!folder) return
    const documentGroups = this.buildDocumentGroups(this.data.documents, this.data.folders, folder.id)
    this.setData({
      currentFolderId: folder.id,
      currentFolderName: folder.name,
      documentGroups,
      visibleDocuments: this.visibleDocumentsOf(documentGroups, folder.id),
    }, () => this.syncCanSend())
  },
  // 按文件夹 id 直接展开（已有 enterFolder 复用同一份逻辑）
  enterFolderById(folderId: string) {
    const folder = this.data.folders.find((f) => f.id === folderId)
    if (!folder) return
    const documentGroups = this.buildDocumentGroups(this.data.documents, this.data.folders, folder.id)
    this.setData({
      currentFolderId: folder.id,
      currentFolderName: folder.name,
      documentGroups,
      visibleDocuments: this.visibleDocumentsOf(documentGroups, folder.id),
    }, () => this.syncCanSend())
  },
  // 面包屑点知识库名＝退回知识库根目录
  exitFolder() {
    const documentGroups = this.buildDocumentGroups(this.data.documents, this.data.folders, '')
    this.setData({
      currentFolderId: '',
      currentFolderName: '',
      documentGroups,
      visibleDocuments: this.visibleDocumentsOf(documentGroups, ''),
    }, () => this.syncCanSend())
  },
  // 目录视图底部输入框：在根目录进知识库问答，在文件夹里进该文件夹问答页（范围由后端锁定）
  openDirectoryAsk() {
    if (this.data.sending || this.data.loadState !== 'ready' || this.data.conversationActive) return
    if (this.data.currentFolderId) { this.openFolderChat(); return }
    this.enterConversationFromComposer()
  },
  openFolderChat() {
    const folderId = this.data.currentFolderId
    if (!folderId || !this.data.knowledgeId) return
    const query = `knowledgeId=${encodeURIComponent(this.data.knowledgeId)}&knowledgeName=${encodeURIComponent(this.data.knowledgeName || '')}&folderId=${encodeURIComponent(folderId)}&folderName=${encodeURIComponent(this.data.currentFolderName || '')}`
    wx.navigateTo({ url: `/package-features/pages/folder-chat/index?${query}` })
  },
  // 目录左滑手势：行卡片左移露出「置顶 / 删除」，一次只展开一行
  onDirTouchStart(e: any) {
    this.setData({ dirTouchStartX: Number(e.touches?.[0]?.clientX || 0), dirTouchStartY: Number(e.touches?.[0]?.clientY || 0) })
  },
  onDirTouchMove(e: any) {
    const touch = e.touches?.[0]
    if (!touch) return
    const dx = Number(touch.clientX || 0) - Number(this.data.dirTouchStartX || 0)
    const dy = Number(touch.clientY || 0) - Number(this.data.dirTouchStartY || 0)
    if (Math.abs(dy) > Math.abs(dx) || Math.abs(dx) < 8) return
    const swipeX = Math.max(-232, Math.min(0, Math.round(dx)))
    this.applyDirSwipe(String(e.currentTarget.dataset.rowtype || ''), String(e.currentTarget.dataset.rowkey || ''), swipeX)
  },
  onDirTouchEnd(e: any) {
    const type = String(e.currentTarget.dataset.rowtype || '')
    const key = String(e.currentTarget.dataset.rowkey || '')
    const swipeX = this.getDirSwipe(type, key)
    this.applyDirSwipe(type, key, swipeX <= -100 ? -232 : 0)
  },
  applyDirSwipe(type: string, key: string, value: number) {
    const documentGroups = this.data.documentGroups.map((group: any) => ({
      ...group,
      swipeX: type === 'folder' && group.key === key ? value : 0,
      documents: (group.documents || []).map((d: any) => ({ ...d, swipeX: type === 'doc' && d.id === key ? value : 0 })),
    }))
    const visibleDocuments = this.data.visibleDocuments.map((d: any) => ({ ...d, swipeX: type === 'doc' && d.id === key ? value : 0 }))
    this.setData({ documentGroups, visibleDocuments })
  },
  getDirSwipe(type: string, key: string): number {
    for (const group of this.data.documentGroups) {
      if (type === 'folder' && group.key === key) return Number(group.swipeX || 0)
      for (const d of group.documents || []) if (type === 'doc' && d.id === key) return Number(d.swipeX || 0)
    }
    return 0
  },
  closeDirSwipe() {
    if (!this.data.documentGroups.length) return
    this.applyDirSwipe('', '', 0)
  },
  pinFolder(e: any) {
    const key = String(e.currentTarget.dataset.key || '')
    if (!key) return
    this.closeDirSwipe()
    this.togglePin('folders', key)
  },
  pinDocument(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    if (!id) return
    this.closeDirSwipe()
    this.togglePin('docs', id)
  },
  togglePin(kind: 'docs' | 'folders', id: string) {
    const state = this.loadPinState()
    const kid = this.data.knowledgeId
    const list = [...(state[kind][kid] || [])]
    const index = list.indexOf(id)
    if (index >= 0) list.splice(index, 1)
    else list.unshift(id)
    state[kind][kid] = list
    this.savePinState(state)
    this.refreshDirectory()
    wx.showToast({ title: index >= 0 ? '已取消置顶' : '已置顶', icon: 'none' })
  },
  confirmDeleteFolder(e: any) {
    const folderId = String(e.currentTarget.dataset.key || '')
    const name = String(e.currentTarget.dataset.name || '')
    if (!folderId) return
    this.closeDirSwipe()
    wx.showModal({
      title: '删除文件夹',
      content: `删除「${name}」后，里面的文件会移回知识库根目录，不会被删除。`,
      confirmText: '删除',
      confirmColor: '#d94545',
      success: (res) => {
        if (!res.confirm) return
        deleteFolder(folderId).then(() => {
          wx.showToast({ title: '已删除', icon: 'success' })
          this.loadDirectory(this.data.knowledgeId)
        }).catch((error: any) => wx.showToast({ title: error.message || '删除失败', icon: 'none' }))
      },
    })
  },
  confirmDeleteDocument(e: any) {
    const documentId = String(e.currentTarget.dataset.id || '')
    const name = String(e.currentTarget.dataset.name || '')
    if (!documentId) return
    this.closeDirSwipe()
    wx.showModal({
      title: '删除文件',
      content: `删除「${name}」后不可恢复，确定删除吗？`,
      confirmText: '删除',
      confirmColor: '#d94545',
      success: (res) => {
        if (!res.confirm) return
        deleteDocument(documentId).then(() => {
          wx.showToast({ title: '已删除', icon: 'success' })
          this.loadDirectory(this.data.knowledgeId)
        }).catch((error: any) => wx.showToast({ title: error.message || '删除失败', icon: 'none' }))
      },
    })
  },
  //（toggleFolderGroup 已由 enterFolder/exitFolder 面包屑导航取代）
  showDocumentActions(e: any) {
    const documentId = String(e.currentTarget.dataset.id || '')
    if (!documentId || !this.data.folders.length) return
    wx.showActionSheet({
      itemList: ['移动到文件夹'],
      success: ({ tapIndex }) => {
        if (tapIndex !== 0) return
        const folders = this.data.folders
        wx.showActionSheet({
          itemList: [...folders.map((f) => f.name), '移出文件夹'],
          success: (res) => {
            const target = res.tapIndex < folders.length ? folders[res.tapIndex].id : ''
            moveDocument(documentId, target).then(() => this.loadDirectory(this.data.knowledgeId)).catch((error: any) => wx.showToast({ title: error.message || '移动失败', icon: 'none' }))
          },
        })
      },
    })
  },
  retryDirectory(e: any) {
    this.loadDirectory(String(e?.currentTarget?.dataset?.id || this.data.knowledgeId))
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
  // 切库时「只读 / 可分享 / 来源已失效」这三个库级状态要跟着一起换：
  // 否则从自己的库切到好友共享过来的库时，上传入口和分享入口会停在上一本库的状态上。
  knowledgeScopeOf(item: any) {
    return { knowledgeReadOnly: !!(item && item.read_only), knowledgeShareable: !!(item && item.shareable), knowledgeSourceMissing: !!(item && item.source_missing) }
  },
  chooseKnowledge(e:any) {
    if (this.data.sending) return
    const item=this.data.knowledge[Number(e.detail.value)]
    if(item) {
      ;(this as any).userLeftConversation = false
      markOverlayClosed(this)
      this.setData({selectedIndex:Number(e.detail.value),knowledgeId:item.id,knowledgeName:item.name,knowledgeDesc:item.description || '',conversationId:'',messages:[],input:'',canSend:false,askMode:'knowledge',currentFolderId:'',currentFolderName:'', ...this.knowledgeScopeOf(item)})
      this.loadDirectory(item.id)
    }
  },
  openKnowledgePicker() {
    if (!isLoggedIn()) { goLogin(); return }
    if (this.data.sending) return
    this.dismissInput()
    if (this.data.loadState !== 'ready') {
      wx.showToast({ title: '资料库正在连接，请稍后重试', icon: 'none' })
      return
    }
    this.setData({ pickerVisible:true, pickerQuery:'', filteredKnowledge:this.data.personalKnowledge, filteredSubscriptions:this.data.subscriptionKnowledge, filteredShared:this.data.sharedKnowledge })
  },
  closeKnowledgePicker() { markOverlayClosed(this); this.setData({ pickerVisible:false, importMode:false }) },
  togglePickerGroup(e:any) {
    const group = String(e.currentTarget.dataset.group || '')
    if (group === 'personal') this.setData({ personalExpanded: !this.data.personalExpanded })
    else if (group === 'shared') this.setData({ sharedExpanded: !this.data.sharedExpanded })
    else if (group === 'subscriptions') this.setData({ subscriptionsExpanded: !this.data.subscriptionsExpanded })
  },
  filterPicker(e:any) {
    const value = String(e.detail.value || '')
    const query = value.trim().toLowerCase()
    this.setData({
      pickerQuery:value,
      personalExpanded:query ? true : this.data.personalExpanded,
      sharedExpanded:query ? true : this.data.sharedExpanded,
      subscriptionsExpanded:query ? true : this.data.subscriptionsExpanded,
      filteredKnowledge:this.data.personalKnowledge.filter(item => item.name.toLowerCase().includes(query)),
      filteredSubscriptions:this.data.subscriptionKnowledge.filter(item => item.name.toLowerCase().includes(query)),
      filteredShared:this.data.sharedKnowledge.filter(item => item.name.toLowerCase().includes(query)),
    })
  },
  selectKnowledge(e:any) {
    const item = this.data.knowledge.find(entry => entry.id === e.currentTarget.dataset.id)
    if (!item) return
    if (this.data.importMode) {
      this.setData({ importMode:false, pickerVisible:false, knowledgeId:item.id, knowledgeName:item.name, knowledgeDesc:item.description || '', conversationId:'', messages:[], selectedIndex:this.data.knowledge.indexOf(item), askMode:'knowledge', ...this.knowledgeScopeOf(item) })
      this.uploadPendingFile(item.id)
      return
    }
    ;(this as any).userLeftConversation = false
    this.setData({ knowledgeId:item.id, knowledgeName:item.name, knowledgeDesc:item.description || '', conversationId:'', messages:[], input:'', canSend:false, pickerVisible:false, selectedIndex:this.data.knowledge.indexOf(item), askMode:'knowledge', currentFolderId:'', currentFolderName:'', ...this.knowledgeScopeOf(item) })
    this.loadDirectory(item.id)
  },
  // 从「选择知识库」里的 + 新建：直接进创建页（不再回到选择弹窗），建好回来就选中它
  createFromPicker() { this.create() },
  create() { if (!isLoggedIn()) { goLogin(); return }; if (this.data.sending) return; this.setData({ pickerVisible:false, importMode:false, uploadSheetVisible:false }); wx.navigateTo({ url:'/package-features/pages/create/index?from=library' }) },
  login() { goLogin() },
  openMine() { wx.switchTab({ url: '/pages/mine/index' }) },
  activate() { if (!isLoggedIn()) { goLogin(); return }; wx.switchTab({ url: '/pages/mine/index' }) },
  goSearch() { if (!isLoggedIn()) { goLogin(); return }; wx.navigateTo({ url: this.data.knowledgeId ? `/package-features/pages/search/index?knowledgeId=${this.data.knowledgeId}` : '/package-features/pages/search/index' }) },
  // 知识库的管理入口：自己的库能分享 / 删除，好友共享过来的库只能移除
  openKnowledgeActions() {
    if (!isLoggedIn()) { goLogin(); return }
    if (!this.data.knowledgeId || this.data.sending) return
    const items: string[] = []
    if (this.data.knowledgeShareable) items.push('分享给微信好友')
    items.push(this.data.knowledgeReadOnly ? '移除这个共享知识库' : '删除知识库')
    wx.showActionSheet({
      itemList: items,
      success: ({ tapIndex }) => {
        const label = items[tapIndex]
        if (label === '分享给微信好友') { this.openKnowledgeShare(); return }
        if (label === '移除这个共享知识库') { this.confirmRemoveShared(); return }
        if (label === '删除知识库') this.confirmDeleteKnowledge()
      },
    })
  },
  // 分享给微信好友：先在服务端换一条邀请链接（一条链接只认第一个接受的好友），
  // 再打开页内面板，用户点「选择微信好友」时由微信拉起转发面板。
  openKnowledgeShare() {
    const id = this.data.knowledgeId
    if (!id || this.data.sending) return
    if (!this.data.knowledgeShareable) { wx.showToast({ title: '这个知识库不能分享给好友', icon: 'none' }); return }
    wx.showLoading({ title: '准备邀请链接', mask: true })
    createKnowledgeShare(id).then((link) => {
      wx.hideLoading()
      this.setData({ kbShareVisible: true, kbShareArmed: true, kbSharePath: link.path, kbShareToken: link.token, kbShareDays: link.days || 7, kbShareTitle: `邀请你一起用「${link.name}」知识库` })
    }).catch((error: any) => {
      wx.hideLoading()
      wx.showToast({ title: (error && error.message) || '邀请链接生成失败，请稍后重试', icon: 'none' })
    })
  },
  closeKnowledgeShare() { this.setData({ kbShareVisible: false, kbShareArmed: false }) },
  // 移除好友共享过来的知识库：只影响自己这边的显示，对方的资料库分毫不动
  confirmRemoveShared() {
    const id = this.data.knowledgeId
    if (!id) return
    wx.showModal({
      title: '移除共享知识库',
      content: `移除后这里不再显示「${this.data.knowledgeName || '这个共享知识库'}」，对方的资料不受影响。`,
      confirmText: '移除',
      success: (res) => { if (res.confirm) this.removeShared(id) },
    })
  },
  removeShared(id: string) {
    deleteKnowledge(id).then(() => {
      wx.showToast({ title: '已移除', icon: 'none' })
      this.setData({ knowledgeId: '', knowledgeName: '', knowledgeDesc: '', documents: [], messages: [], conversationId: '', conversationActive: false })
      this.load()
    }).catch(() => wx.showToast({ title: '移除失败，请稍后重试', icon: 'none' }))
  },
  confirmDeleteKnowledge() {
    wx.showModal({
      title: '删除知识库',
      content: `删除后「${this.data.knowledgeName || '当前知识库'}」及其中的文档将无法恢复，确定删除吗？`,
      confirmText: '删除',
      confirmColor: '#c45d5d',
      success: (res) => { if (res.confirm) this.removeKnowledge() },
    })
  },
  removeKnowledge() {
    const id = this.data.knowledgeId
    if (!id) return
    deleteKnowledge(id).then(() => {
      wx.showToast({ title: '已删除知识库', icon: 'success' })
      this.setData({ knowledgeId: '', knowledgeName: '', knowledgeDesc: '', documents: [], messages: [], conversationId: '', conversationActive: false })
      this.load()
    }).catch(() => wx.showToast({ title: '删除失败，请稍后重试', icon: 'none' }))
  },
  openAgreement() { this.closeKnowledgePicker(); wx.navigateTo({ url: '/package-features/pages/legal/index?type=terms' }) },
  openPrivacy() { this.closeKnowledgePicker(); wx.navigateTo({ url: '/package-features/pages/legal/index?type=guide' }) },
  openAiPrivacy() { this.closeKnowledgePicker(); wx.navigateTo({ url: '/package-features/pages/legal/index?type=ai-privacy' }) },
  leaveConversation() {
    this.detachStream()
    if (this.data.pickMode) pickPage.exit(this)
    // 用户主动返回目录后，本次页面存续期内不再自动拉回复会话
    ;(this as any).userLeftConversation = true
    this.setData({ conversationId: '', conversationActive: false, messages: [], input: '', sending: false, modePickerVisible: false, modelPickerVisible:false, lastMessageId:'', scrollTop: 0 })
    resetTail(this)
    this.syncCanSend()
  },
  // 开场白：先说清「在哪个范围内问答」，再由推荐问题引导第一句提问（本地消息，不写入历史）
  buildGuideMessage(mode?: 'knowledge' | 'web') {
    const id = `guide-${Date.now()}`
    const askMode = mode || this.data.askMode
    const kbName = this.data.knowledgeName || '微信用户的知识库'
    const isEmpty = !this.data.documentsLoading && !this.data.documents.length
    const title = '你好，我是 cola'
    let desc: string
    if (askMode === 'web') {
      desc = '当前是全网问答，我会先检索公开资料再回答你的问题。'
    } else if (isEmpty) {
      desc = `这里是「${kbName}」，目前还没有资料。导入文件后就能基于资料问答，也可以直接提问。`
    } else {
      desc = `这里是「${kbName}」，共 ${this.data.documents.length} 份资料，提问时我会只依据这些资料回答。`
    }
    return { id, role: 'assistant', local: true, greeting: true, title, desc, sources: [], trace: [], reason: '', content: `${title}。${desc}` }
  },
  // 目录视图下点击输入框：有历史会话则直接恢复进入对话页，否则进入 ima 式提问层
  enterConversationFromComposer() {
    if (this.data.conversationActive || this.data.sending || this.data.loadState !== 'ready') return
    this.restoreLatestConversation(this.data.knowledgeId, () => this.openAskLayer())
  },
  openAskLayer(draft = '') {
    if (this.data.sending || this.data.loadState !== 'ready' || this.data.askLayerVisible) return
    const kbName = this.data.knowledgeName || '微信用户的知识库'
    const isEmpty = !this.data.documentsLoading && !this.data.documents.length
    const askGreeting = this.data.askMode === 'web'
      ? 'Hi，想了解什么？cola 可以帮你搜全网，尽管问。'
      : (isEmpty ? `Hi，这是「${kbName}」。当前还没有文件，先导入文档后就能基于资料提问啦。` : `Hi，有任何关于「${kbName}」的问题，都尽管问cola！`)
    this.setData({ askLayerVisible: true, modePickerVisible: false, askGreeting, input: draft }, () => {
      this.syncCanSend()
      if (this.data.askMode === 'knowledge' && this.data.suggestionsFor !== this.data.knowledgeId) this.loadSuggestions()
      setTimeout(() => { if (this.data.askLayerVisible) this.setData({ askFocus: true }) }, 250)
    })
  },
  closeAskLayer() { markOverlayClosed(this); this.setData({ askLayerVisible: false, askFocus: false }) },
  loadSuggestions() {
    if (!this.data.knowledgeId) return
    this.setData({ suggestionsLoading: true })
    getSuggestions(this.data.knowledgeId).then((result: any) => {
      this.setData({ suggestions: (result && result.questions) || [], suggestionsFor: this.data.knowledgeId, suggestionsLoading: false })
    }).catch(() => this.setData({ suggestionsLoading: false }))
  },
  chooseSuggestion(e: any) {
    const question = this.data.suggestions[Number(e.currentTarget.dataset.index)] || ''
    if (!question) return
    // 点推荐问题=直接发送，不再回填输入框
    this.setData({ input: question }, () => this.sendFromAsk())
  },
  // 开场白出现时才需要推荐问题：同一个知识库只拉一次
  ensureSuggestions() {
    if (!this.data.knowledgeId || this.data.suggestionsLoading) return
    if (this.data.suggestionsFor === this.data.knowledgeId && this.data.suggestions.length) return
    this.loadSuggestions()
  },
  // 会话开场后的提问建议：点了直接发，不再回填输入框
  pickPrompt(e: any) {
    const question = String(e.currentTarget.dataset.text || '')
    if (!question || this.data.sending) return
    this.setData({ input: question, readyForInput: true }, () => this.send())
  },
  sendFromAsk() {
    if (!this.data.input.trim()) return
    this.setData({ askLayerVisible: false, askFocus: false }, () => {
      if (this.data.conversationActive) { this.send(); return }
      // 首次提问：先带问候语进入会话，再发送
      const guide = this.buildGuideMessage(this.data.askMode)
      this.setData({ conversationActive: true, conversationId: '', messages: [guide], lastMessageId: guide.id }, () => this.send())
    })
  },
  togglePinned() {
    this.setData({ pinned: !this.data.pinned })
    wx.showToast({ title: this.data.pinned ? '已固定会话' : '已取消固定', icon: 'none' })
  },
  // 复制回答：只取本轮输出的纯文本（与渲染同一套分块，去掉 Markdown 标记），直接进剪贴板
  copyAnswer(e: any) {
    const id = String((e.currentTarget.dataset || {}).id || '')
    const message = (this.data.messages as any[]).find((item) => item && item.id === id)
    const raw = String((message && message.content) || '').trim()
    if (!raw) { wx.showToast({ title: '这条回答还没有内容', icon: 'none' }); return }
    copyText(markdownToText(raw) || raw, '回答已复制')
  },
  // 分享给微信好友：点「分享」直接拉起转发面板，卡片里带这条回答的分享页
  onShareAppMessage(e: any): any {
    // 知识库邀请：面板上点「选择微信好友」时转发的是这个知识库的邀请链接
    if (this.data.kbShareArmed && this.data.kbSharePath) {
      const payload = { title: this.data.kbShareTitle || 'cola知识库', path: this.data.kbSharePath, imageUrl: SHARE_IMAGE }
      this.setData({ kbShareVisible: false, kbShareArmed: false })
      return payload
    }
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
  toggleModePicker() { if (!this.data.sending) this.setData({ modePickerVisible: !this.data.modePickerVisible }) },
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
  // 产物卡在多选态下改成勾选，不在多选态才走原来的打开逻辑
  onFileTap(e: any) {
    const id = String((e.currentTarget.dataset || {}).id || '')
    if (this.data.pickMode) { pickPage.toggle(this, pickPage.fileKey(id)); return }
    this.openArtifact(e)
  },
  selectAskMode(e: any) {
    const mode = e.currentTarget.dataset.mode as 'knowledge' | 'web'
    this.setData({
      askMode: mode,
      modePickerVisible: false,
      input: '',
    }, () => {
      if (this.data.conversationActive && this.data.messages.length === 1 && this.data.messages[0].local) {
        const guide = this.buildGuideMessage(mode)
        this.setData({ messages:[guide], lastMessageId:guide.id })
      }
      this.syncCanSend()
    })
  },
  newConversation() {
    this.detachStream()
    const mode = this.data.askMode
    const guide = this.buildGuideMessage(mode)
    this.setData({
      conversationId:'',
      conversationActive:true,
      messages:[guide],
      input:'',
      canSend:false,
      sending:false,
      readyForInput:true,
      lastMessageId:guide.id,
      askMode:mode,
    })
    this.syncCanSend()
  },
  // 历史回放走 hydrateAssistant：思考全文、过程节点、耗时都从服务端复原，与实时流一致
  loadConversation() {
    const conversationId = this.data.conversationId
    this.detachStream()
    getConversation(conversationId).then((messages) => {
      const hydrated = messages.map((m: any) => m.role === 'assistant' ? { ...hydrateAssistant(m), progress: '' } : m)
      this.setData({ messages: hydrated, lastMessageId: '' }, () => { resetTail(this); pinTailSoon(this) })
      this.resumeActiveRun(conversationId)
    }).catch(() => undefined)
  },
  // 恢复当前知识库最近一次会话；异步返回时用户可能已切库/已输入/已主动离开，恢复前需再校验。
  // 文件夹会话在独立文件夹页内恢复，这里只挑根目录（folder_id 为空）的会话，保证隔离
  restoreLatestConversation(knowledgeId: string, onNoHistory?: () => void) {
    getConversations().then((items) => {
      const latest = (items || []).find((item: any) => item.knowledge_id === knowledgeId && !(item.folder_id || ''))
      if (!latest) {
        if (onNoHistory) onNoHistory()
        return
      }
      if ((this as any).userLeftConversation || this.data.conversationActive || this.data.messages.length || this.data.knowledgeId !== knowledgeId) return
      this.setData({ conversationId: latest.id, conversationActive: true })
      this.loadConversation()
    }).catch(() => { if (onNoHistory) onNoHistory() })
  },
  syncCanSend() { this.setData({ canSend: !!this.data.readyForInput && !!this.data.input.trim() && (this.data.askMode === 'web' || !!this.data.knowledgeId) && this.data.loadState === 'ready' && !this.data.sending }) },
  onInput(e: any) {
    const next = { input: e.detail.value, readyForInput: true } as any
    // 仅提问层内输入才切换到会话态；首页窄输入框输入保持原位，避免视图跳变打断键盘
    if (!this.data.conversationActive && this.data.askLayerVisible) {
      const mode = this.data.askMode
      const guide = this.buildGuideMessage(mode)
      Object.assign(next, { conversationActive: true, conversationId: '', messages: [guide], lastMessageId: guide.id, modePickerVisible: false, askMode: mode })
    }
    this.setData(next)
    // 技能只随请求下发，不再往输入框里塞模板：改写输入不会误删已选技能
    this.syncCanSend()
  },
  useSuggestion(e: any) {
    // Suggestions are an input shortcut, not an implicit submit action.
    // The user must review the text and tap the send button explicitly.
    if (this.data.sending || !this.data.knowledgeId || this.data.loadState !== 'ready') return
    this.setData({
      conversationActive: true,
      conversationId: '',
      messages: [this.buildGuideMessage(this.data.askMode)],
      lastMessageId: '',
      modePickerVisible: false,
      readyForInput: true,
      input: String(e.currentTarget.dataset.text || ''),
    }, () => this.syncCanSend())
  },
  chooseModel() { if (!this.data.sending) { this.dismissInput(); this.setData({ modelPickerVisible:true, modePickerVisible:false, pickerVisible:false }) } },
  closeModelPicker() { if (!this.data.sending) { markOverlayClosed(this); this.setData({ modelPickerVisible:false }) } },
  stopModelPickerBubble() { return },
  // 与问AI问答页一致：模型页只留一个深度思考开关
  toggleDeepThinking() {
    if (this.data.sending) return
    this.setData({ thinkingMode: this.data.thinkingMode === 'deep' ? 'quick' : 'deep' })
  },
  selectThinkingMode(e: any) {
    if (this.data.sending) return
    this.setData({ thinkingMode: e.currentTarget.dataset.mode as 'quick' | 'deep' })
  },
  loadModels() {
    getModels().then((options) => {
      if (!options || !options.length) return
      const current = options.find((item) => item.id === this.data.selectedModelKey) || options[0]
      this.setData({ modelOptions: options, selectedModelKey: current.id, model: current.value, modelLabel: current.name, modelShortLabel: current.short })
    }).catch(() => {})
  },
  selectModel(e: any) {
    if (this.data.sending) return
    const selected = this.data.modelOptions.find((item: any) => item.id === e.currentTarget.dataset.id)
    if (!selected) return
    this.setData({ selectedModelKey: selected.id, model: selected.value, modelLabel: selected.name, modelShortLabel: selected.short, modelPickerVisible:false })
  },
  openKnowledgeDetail() { if (this.data.knowledgeId) wx.navigateTo({ url: `/package-features/pages/knowledge-detail/index?id=${this.data.knowledgeId}` }) },
  openDocument(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    if (!id) return
    // 行已左滑展开时，第一次点击先收起而不是打开文档
    if (this.getDirSwipe('doc', id) < 0) { this.applyDirSwipe('doc', id, 0); return }
    wx.navigateTo({ url: `/package-features/pages/document/index?id=${id}` })
  },
  openDiscover() { if (!isLoggedIn()) { goLogin(); return }; this.closeKnowledgePicker(); wx.navigateTo({ url: '/package-features/pages/market/index' }) },
  async openUpload() {
    if (!isLoggedIn()) { goLogin(); return }
    if (!this.data.knowledgeId || this.data.sending) return
    // 好友共享过来的库是只读镜像：资料由分享者维护，这里不允许添加 / 删除
    if (this.data.knowledgeReadOnly) { wx.showToast({ title: '好友共享的知识库只能阅读和提问', icon: 'none' }); return }
    this.setData({ uploadSheetVisible:true })
    this.syncTabBar()
  },
  closeUploadSheet() {
    if (this.data.sending) return
    this.setData({ uploadSheetVisible:false })
    this.syncTabBar()
  },
  // 相册、拍照、微信文件都是隐私接口：上传前先给一个随时可读的《小程序用户隐私保护指引》入口。
  openPrivacyGuide() { wx.navigateTo({ url: '/package-features/pages/legal/index?type=guide' }) },
  async chooseUploadSource(e: any) {
    const source = String(e.currentTarget.dataset.source || '')
    if (!source || this.data.sending) return
    if (source === 'knowledge') {
      this.create()
      return
    }
    if (source === 'folder') {
      this.setData({ uploadSheetVisible:false })
      this.promptCreateFolder()
      return
    }
    if (source === 'article') {
      this.setData({ uploadSheetVisible:false, articleSheetVisible:true, articleUrl:'', articleImporting:false })
      this.syncTabBar()
      return
    }
    this.setData({ uploadSheetVisible:false })
    this.pickTargetFolder(this.data.folders, async (folderId) => {
      try {
        const result = await uploadDocument(this.data.knowledgeId, source as UploadSource, folderId)
        wx.showToast({ title: result.status === 'completed' ? '已上传并完成解析' : '已上传，正在解析', icon: result.status === 'completed' ? 'success' : 'none' })
        this.load()
      } catch (error: any) {
        if (error?.cancelled || String(error?.errMsg || error?.message || '').includes('cancel')) return
        if (error?.privacy) { warnPrivacyRequired('导入资料'); return }
        wx.showToast({ title: error?.message || '上传失败，请稍后重试', icon: 'none' })
      }
    })
  },
  // 上传/导入的目标位置：已经站在某个文件夹里就直接放进去，不再问一次；
  // 只有还在知识库根目录、且确实存在文件夹时才让用户选。
  pickTargetFolder(folders: Folder[], action: (folderId: string) => void) {
    if (this.data.currentFolderId) { action(this.data.currentFolderId); return }
    if (!folders.length) { action(''); return }
    wx.showActionSheet({
      itemList: ['知识库根目录', ...folders.map((f) => f.name)],
      success: ({ tapIndex }) => action(tapIndex === 0 ? '' : folders[tapIndex - 1].id),
    })
  },
  promptCreateFolder() {
    if (!this.data.knowledgeId) return
    wx.showModal({
      title: '创建文件夹',
      placeholderText: '请输入文件夹名称',
      editable: true,
      confirmText: '创建',
      success: (res) => {
        if (!res.confirm) return
        const name = String(res.content || '').trim()
        if (!name) { wx.showToast({ title: '名称不能为空', icon: 'none' }); return }
        createFolder(this.data.knowledgeId, name).then(() => {
          wx.showToast({ title: '文件夹已创建', icon: 'success' })
          this.loadDirectory(this.data.knowledgeId)
        }).catch((error: any) => wx.showToast({ title: error.message || '创建失败', icon: 'none' }))
      },
    })
  },
  onArticleUrlInput(e: any) { this.setData({ articleUrl: String(e.detail.value || '').trim() }) },
  clearArticleUrl() { this.setData({ articleUrl: '' }) },
  closeArticleSheet() { if (this.data.articleImporting) return; this.setData({ articleSheetVisible:false }); this.syncTabBar() },
  async confirmArticleImport() {
    const url = this.data.articleUrl.trim()
    if (!url || this.data.articleImporting || !this.data.knowledgeId) return
    if (!/^https?:\/\/mp\.weixin\.qq\.com\//.test(url)) { wx.showToast({ title: '请粘贴公众号文章链接（mp.weixin.qq.com）', icon: 'none' }); return }
    this.pickTargetFolder(this.data.folders, async (folderId) => {
      this.setData({ articleImporting:true })
      try {
        const result = await importArticle(this.data.knowledgeId, url, folderId)
        this.setData({ articleSheetVisible:false, articleImporting:false, articleUrl:'' })
        // 后端先登记 processing 再后台抓取：拿到 id 就是受理成功，标题与正文稍后才落库
        const accepted = !!(result && result.id)
        wx.showToast({ title: accepted ? '已开始导入，正在抓取文章…' : '导入失败，请重试', icon: accepted ? 'success' : 'none', duration: 2500 })
        this.load()
      } catch (error: any) {
        this.setData({ articleImporting:false })
        wx.showToast({ title: error.message || '导入失败，请检查链接后重试', icon: 'none', duration: 2500 })
      }
    })
  },
  // 小机器人两态互切：执行规划（联网/ agent）与基于知识库问答；第三、第四个图标随状态换形
  toggleAskLogic() {
    if (this.data.sending) return
    const next = this.data.askMode === 'web' ? 'knowledge' : 'web'
    if (next === 'knowledge' && !this.data.knowledgeId) { wx.showToast({ title: '请先创建知识库', icon: 'none' }); return }
    this.setData({ askMode: next, modePickerVisible: false, skillSheetVisible: false, pickerVisible: false, modelPickerVisible: false }, () => this.syncCanSend())
  },
  openSkillSheet() {
    if (this.data.sending) return
    this.dismissInput()
    this.setData({ skillSheetVisible: true, modePickerVisible: false, pickerVisible: false, modelPickerVisible: false })
  },
  closeSheets() { markOverlayClosed(this); this.setData({ skillSheetVisible: false }) },
  // 弹层内部点击不穿透到遮罩
  noop() { return },
  // 多选：点一下切换一个技能，面板不关闭；选择持久化，发送与切换视图都不会重置
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
  clearSkill() { this.applySkills([]) },
  restoreSkillPrefs() {
    restoreSkills().then((ids) => {
      const patch: any = { skillsReady: true }
      // 用户已经手动改过技能时，远端回包不能覆盖刚做的选择。
      if (!(this.data as any).skillsTouched) patch.selectedSkillIds = ids
      this.setData(patch)
    }).catch(() => this.setData({ skillsReady: true }))
  },
  // 新建 / 编辑技能：收起面板与跳转同一拍发出。
  // 等面板收起的渲染回调再跳，会让整个消息列表先重绘一遍，点下去要愣半秒才进页。
  onSkillCreate() { this.setData({ skillSheetVisible: false }); wx.navigateTo({ url: '/package-features/pages/skill-edit/index' }) },
  onSkillEdit(e: any) {
    const id = String((e.detail && e.detail.id) || '')
    if (!id) return
    this.setData({ skillSheetVisible: false }); wx.navigateTo({ url: `/package-features/pages/skill-edit/index?id=${encodeURIComponent(id)}` })
  },
  // 历史对话抽屉：数据来自服务端 /api/conversations，按用户 + 知识库收窄，只显示根目录会话
  openHistory() {
    if (!isLoggedIn()) { goLogin(); return }
    this.dismissInput()
    this.setData({ historyVisible: true, historyLoading: true })
    getConversations({ knowledgeId: this.data.knowledgeId, folderId: '' }).then((items) => {
      this.setData({ historyItems: items || [], historyLoading: false })
    }).catch(() => this.setData({ historyItems: [], historyLoading: false }))
  },
  closeHistory() { markOverlayClosed(this); this.setData({ historyVisible: false }) },
  // 左滑「置顶 / 取消置顶」：只改当前用户自己的会话，置顶后排到列表最前。
  pinHistory(e: any) {
    const id = String((e.detail && e.detail.id) || '')
    const pinned = !!(e.detail && e.detail.pinned)
    if (!id) return
    pinConversation(id, pinned).then(() => {
      const items = (this.data as any).historyItems.map((item: any) => item.id === id ? { ...item, pinned: pinned ? 1 : 0 } : item)
      this.setData({ historyItems: items })
      wx.showToast({ title: pinned ? '已置顶' : '已取消置顶', icon: 'none' })
    }).catch(() => wx.showToast({ title: '操作失败，请稍后重试', icon: 'none' }))
  },
  pickHistory(e: any) {
    const id = String((e.detail && e.detail.id) || '')
    if (!id) return
    this.detachStream()
    ;(this as any).userLeftConversation = false
    this.setData({ historyVisible: false, conversationId: id, conversationActive: true, messages: [], sending: false, canSend: false, lastMessageId: '' })
    resetTail(this)
    this.loadConversation()
  },
  // 左滑出「删除」；会话在服务端，删前必须二次确认
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
          const items = (this.data as any).historyItems.filter((item: any) => item.id !== id)
          const isCurrent = this.data.conversationId === id
          this.setData({ historyItems: items, conversationId: isCurrent ? '' : this.data.conversationId })
          if (isCurrent) this.setData({ messages: [], lastMessageId: '' })
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
    const mode = this.data.askMode
    if (mode === 'knowledge' && !this.data.knowledgeId) { wx.showToast({ title: '请先创建资料库', icon: 'none' }); return }
    // 知识库为空也允许直接提问：后端在没有资料时会退化为通用能力回答，
    // 不再弹「是否切换到全网」这类打断式提示。
    this.doSend(mode, content)
  },
  doSend(mode: 'knowledge' | 'web', content: string) {
    const userId = `m${Date.now()}`; const assistantId = `m${Date.now() + 1}`
    // 技能是一段长期设定：发送后保留，只有用户在面板里改动才会变
    const skills = this.data.selectedSkillIds
    this.setData({ input: '', conversationActive:true, sending: true, canSend: false, messages: [...this.data.messages, { id: userId, role: 'user', content, sources: [] }, assistantMessage(assistantId)], lastMessageId: assistantId }, () => { resetTail(this); pinTail(this) })
    let assistant = ''
    const payload: any = { mode, conversation_id: this.data.conversationId || undefined, content, model: this.data.model, thinking: this.data.thinkingMode }
    // 选中的技能随请求下发（可多选），后端先加载并注入技能再执行任务
    payload.skills = skills
    if (mode === 'knowledge') payload.knowledge_id = this.data.knowledgeId
    ;(this as any).cancelStream = streamChat(
      payload,
      (meta) => { this.setData({ conversationId: meta.conversation_id, chatRunId: (meta.run && meta.run.id) || (this as any).chatRunId || '' }); this.updateAssistant(assistantId, assistant, meta.sources as Source[]) },
      (delta) => { assistant += delta; this.flushDelta(assistantId, () => assistant) },
      () => { (this as any).cancelStream = null; (this as any).chatRunId = ''; this.flushReset(assistantId); this.updateAssistant(assistantId, assistant); this.settleAnswer(assistantId); this.setData({ sending: false }); this.syncCanSend() },
      (error) => { (this as any).cancelStream = null; (this as any).chatRunId = ''; this.flushReset(assistantId); this.updateAssistant(assistantId, assistant || '回答未完成，请稍后重新提问。'); this.settleAnswer(assistantId); this.setData({ sending: false }); this.syncCanSend(); wx.showToast({ title: error.message || error.errMsg || '回答失败', icon: 'none' }) },
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
  // 过程区与增量节流统一走 utils/thread，与问AI页、文件夹会话共用同一实现
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
  stopSend() {
    const runId = String((this as any).chatRunId || '')
    if (runId) stopChatRun(runId).catch(() => undefined)
    this.detachStream()
    const messages = this.data.messages
    const len = messages.length
    // 用户主动停止：移除本轮未完成的占位回答（连带前一条用户提问也撤销，避免半截对话留在界面上）
    const dropLastPair = len >= 2 && messages[len - 1].role === 'assistant' && !messages[len - 1].content && messages[len - 2].role === 'user'
    const nextMessages = dropLastPair ? messages.slice(0, len - 2) : messages
    this.setData({
      sending: false,
      messages: nextMessages,
      lastMessageId: nextMessages.length ? nextMessages[nextMessages.length - 1].id : ''
    }, () => this.syncCanSend())
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
      this.setData({ messages, lastMessageId: '', conversationActive: true, sending: true, canSend: false, chatRunId: run.id }, () => { this.syncCanSend(); resetTail(this); pinTailSoon(this) })
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
  onUnload() {
    const cancel = (this as any).cancelStream; if (cancel) cancel()
    const flusher = (this as any).flusher; if (flusher) flusher.reset()
  },
  detachStream() {
    const cancel = (this as any).cancelStream
    if (cancel) cancel()
    ;(this as any).cancelStream = null
    ;(this as any).chatRunId = ''
  },
  updateAssistant(id: string, content: string, sources?: Source[]) { const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, content, progress: content ? '' : message.progress, sources: sources ? decorateSources(sources) : message.sources } : message); this.setData({ messages, lastMessageId: id }); pinTail(this) },
  updateAssistantProgress(id: string, progress: string) { const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, progress } : message); this.setData({ messages }) },
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
    if (url) {
      wx.setClipboardData({ data: url, success: () => wx.showToast({ title: '网页链接已复制', icon: 'none' }) })
      return
    }
    if (id) wx.navigateTo({ url: `/package-features/pages/document/index?id=${id}` })
  },
})
