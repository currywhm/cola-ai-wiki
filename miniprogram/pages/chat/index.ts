import { consumeChatTarget } from '../../services/navigation'
import { addArtifact, appendTrace, assistantMessage, createFlusher, decorateSources, hydrateAssistant, markPlanReviewed, settleTrace, togglePlan } from '../../utils/thread'
// 生成的文件：卡片打开 / 保存到微信都在 utils/artifact 里统一实现，三个会话页共用同一份
import { openArtifact as openArtifactFile, saveArtifact as saveArtifactFile } from '../../utils/artifact'
import { renderMarkdown } from '../../utils/markdown'
import { fileTypeKind, fileTypeLabel } from '../../utils/file-type'
import { deleteConversation, getConversation, getConversations, getKnowledge, getKnowledgeDetail, deleteDocument, deleteKnowledge, getModels, getFolders, getSuggestions, createFolder, deleteFolder, importArticle, moveDocument, pinConversation, uploadLocalFile, Knowledge, ModelOption, Folder, resumeWechatLogin, Source, reviewPlan, streamChat, uploadDocument, UploadSource } from '../../services/api'
import { buildSharePayload, homePayload, questionFor } from '../../utils/share'
import { persistSkills, restoreSkills } from '../../utils/skill-prefs'

const DEFAULT_KNOWLEDGE_NAME = '微信用户的知识库'
// 与后端 CHAT_MODELS 一致的兜底清单；正常运行时会被 /api/models 的返回值覆盖
const FALLBACK_MODEL_OPTIONS: ModelOption[] = [
  { id: 'deepseek-flash', name: '云枢', value: 'deepseek-flash', badge: '', short: '云枢' },
  { id: 'deepseek-v4-pro', name: '墨衡', value: 'deepseek-v4-pro', badge: '', short: '墨衡' },
]
// 启动页已上移到小程序入口（问AI tab）：知识库页面不再拦截首屏，这里保留空标记保证旧调用安全
let bootSplashShown = true


Page({
  data: { safeBottom: 0, dirTouchStartX: 0, dirTouchStartY: 0, booting: !bootSplashShown, knowledgeId: '', knowledgeName: '', knowledgeDesc: '', conversationId: '', conversationActive: false, selectedIndex: 0, input: '', canSend: false, sending: false, readyForInput: false, lastMessageId: '', model: 'deepseek-flash', selectedModelKey: 'deepseek-flash', modelLabel: '云枢', modelShortLabel: '云枢', thinkingMode: 'quick' as 'quick' | 'deep', modelOptions: FALLBACK_MODEL_OPTIONS, modelPickerVisible: false, askMode: 'knowledge' as 'knowledge' | 'web', modePickerVisible: false, pinned: false, uploadSheetVisible: false, uploadUsedLabel: '0.00GB', uploadLimitLabel: '300MB', loadState:'loading', knowledge:[] as Knowledge[], filteredKnowledge:[] as Knowledge[], pickerQuery:'', pickerVisible:false, documents:[] as any[], documentsLoading:false, documentsError:false, folders:[] as Folder[], documentGroups:[] as any[], visibleDocuments:[] as any[], currentFolderId:'', currentFolderName:'', articleSheetVisible:false, articleUrl:'', articleImporting:false, importMode:false, pendingFileName:'', askLayerVisible:false, askFocus:false, askGreeting:'', suggestions:[] as string[], suggestionsFor:'', suggestionsLoading:false, messages: [] as any[], selectedSkillIds: [] as string[] },
  onLoad() {
    // tabBar 为自定义组件：会话态、提问层、进入文件夹后的目录都要隐藏它，
    // 这里统一拦截 setData 同步，避免逐个调用点遗漏
    const originalSetData = this.setData.bind(this)
    const tabBarKeys = ['conversationActive', 'askLayerVisible', 'currentFolderId']
    ;(this as any).setData = (data: any, callback?: () => void) => {
      originalSetData(data, () => {
        if (data && tabBarKeys.some((key) => Object.prototype.hasOwnProperty.call(data, key))) this.syncTabBar()
        // 会话里只剩下开场白时，顺手拉一次推荐问题（后端按指纹缓存）
        if (data && Array.isArray(data.messages) && data.messages.length === 1 && data.messages[0] && data.messages[0].greeting) this.ensureSuggestions()
        if (callback) callback()
      })
    }
    this.primeThreadState()
  },
  // 自定义 tabBar 需要页面自己同步选中项与显隐：会话视图为全屏，隐藏底部导航
  // 会话视图与问AI问答页共用一套交互：技能包、历史对话抽屉、顶部安全高度
  primeThreadState() {
    // selectedSkillIds 在 restoreSkillPrefs 之前先给出空数组，避免异步返回前的空值访问
    this.setData({ navHeight: 88, skillSheetVisible: false, selectedSkillIds: [] as string[], historyVisible: false, historyLoading: false, historyItems: [] })
    this.restoreSkillPrefs()
    this.measureNav()
  },
  syncTabBar() {
    if (typeof this.getTabBar !== 'function') return
    const bar = this.getTabBar() as any
    if (!bar || typeof bar.setData !== 'function') return
    // 只有四个 tab 首页保留底部导航：会话页、提问层、以及「进入文件夹后的目录」都是从首页钻进去的视图
    bar.setData({ selected: 1, hidden: !!(this.data.conversationActive || this.data.askLayerVisible || this.data.currentFolderId) })
  },
  onShow() {
    this.measureNav()
    this.syncTabBar()
    this.measureSafeArea()
    const target = consumeChatTarget()
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
  },
  finishBoot() {
    // 启动页已上移到小程序入口（问AI tab）：知识库页不再有首屏加载动画
  },
  load(loadConversation = false) {
    this.setData({ loadState:'loading', readyForInput:false, canSend:false })
    this.loadModels()
    getKnowledge().then((knowledge) => {
      const currentIndex = knowledge.findIndex(k=>k.id===this.data.knowledgeId)
      const defaultIndex = knowledge.findIndex(k=>k.name === DEFAULT_KNOWLEDGE_NAME)
      const selectedIndex = currentIndex >= 0 ? currentIndex : (defaultIndex >= 0 ? defaultIndex : 0)
      const current=knowledge[selectedIndex]
      this.setData({ knowledge, filteredKnowledge:knowledge, selectedIndex, loadState:'ready', knowledgeId:current ? current.id : '', knowledgeName:current ? current.name : '', knowledgeDesc:current ? (current.description || '') : '' }, () => {
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
    this.setData({ importMode: true, pendingFileName: file.filename, pickerVisible: true, pickerQuery: '', filteredKnowledge: this.data.knowledge })
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
    wx.navigateTo({ url: `/pages/folder-chat/index?${query}` })
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
  chooseKnowledge(e:any) {
    if (this.data.sending) return
    const item=this.data.knowledge[Number(e.detail.value)]
    if(item) {
      ;(this as any).userLeftConversation = false
      this.setData({selectedIndex:Number(e.detail.value),knowledgeId:item.id,knowledgeName:item.name,knowledgeDesc:item.description || '',conversationId:'',messages:[],input:'',canSend:false,askMode:'knowledge',currentFolderId:'',currentFolderName:''})
      this.loadDirectory(item.id)
    }
  },
  openKnowledgePicker() {
    if (this.data.sending) return
    if (this.data.loadState !== 'ready') {
      wx.showToast({ title: '资料库正在连接，请稍后重试', icon: 'none' })
      return
    }
    this.setData({ pickerVisible:true, pickerQuery:'', filteredKnowledge:this.data.knowledge })
  },
  closeKnowledgePicker() { this.setData({ pickerVisible:false, importMode:false }) },
  filterPicker(e:any) { const query = String(e.detail.value || '').trim().toLowerCase(); this.setData({ pickerQuery:e.detail.value, filteredKnowledge:this.data.knowledge.filter(item => item.name.toLowerCase().includes(query)) }) },
  selectKnowledge(e:any) {
    const item = this.data.knowledge.find(entry => entry.id === e.currentTarget.dataset.id)
    if (!item) return
    if (this.data.importMode) {
      this.setData({ importMode:false, pickerVisible:false, knowledgeId:item.id, knowledgeName:item.name, knowledgeDesc:item.description || '', conversationId:'', messages:[], selectedIndex:this.data.knowledge.indexOf(item), askMode:'knowledge' })
      this.uploadPendingFile(item.id)
      return
    }
    ;(this as any).userLeftConversation = false
    this.setData({ knowledgeId:item.id, knowledgeName:item.name, knowledgeDesc:item.description || '', conversationId:'', messages:[], input:'', canSend:false, pickerVisible:false, selectedIndex:this.data.knowledge.indexOf(item), askMode:'knowledge', currentFolderId:'', currentFolderName:'' })
    this.loadDirectory(item.id)
  },
  createFromPicker() { this.closeKnowledgePicker(); this.create() },
  create() { wx.navigateTo({url:'/pages/create/index'}) },
  login() {
    this.setData({ loadState:'loading' })
    resumeWechatLogin().then(() => this.load()).catch(() => this.setData({ loadState:'error' }))
  },
  openMine() { wx.switchTab({ url: '/pages/mine/index' }) },
  activate() { wx.switchTab({ url: '/pages/mine/index' }) },
  goSearch() { wx.navigateTo({ url: this.data.knowledgeId ? `/pages/search/index?knowledgeId=${this.data.knowledgeId}` : '/pages/search/index' }) },
  openKnowledgeActions() {
    if (!this.data.knowledgeId || this.data.sending) return
    wx.showActionSheet({
      itemList: ['删除知识库'],
      success: ({ tapIndex }) => { if (tapIndex === 0) this.confirmDeleteKnowledge() },
    })
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
  openAgreement() { this.closeKnowledgePicker(); wx.navigateTo({ url: '/pages/legal/index?type=agreement' }) },
  openPrivacy() { this.closeKnowledgePicker(); wx.navigateTo({ url: '/pages/legal/index?type=privacy' }) },
  leaveConversation() {
    if (this.data.sending) return
    // 用户主动返回目录后，本次页面存续期内不再自动拉回复会话
    ;(this as any).userLeftConversation = true
    this.setData({ conversationId: '', conversationActive: false, messages: [], input: '', modePickerVisible: false, modelPickerVisible:false, lastMessageId:'' })
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
  closeAskLayer() { this.setData({ askLayerVisible: false, askFocus: false }) },
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
  toggleModePicker() { if (!this.data.sending) this.setData({ modePickerVisible: !this.data.modePickerVisible }) },
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
    if (this.data.sending) return
    const mode = this.data.askMode
    const guide = this.buildGuideMessage(mode)
    this.setData({
      conversationId:'',
      conversationActive:true,
      messages:[guide],
      input:'',
      canSend:false,
      readyForInput:true,
      lastMessageId:guide.id,
      askMode:mode,
    })
    this.syncCanSend()
  },
  // 历史回放走 hydrateAssistant：思考全文、过程节点、耗时都从服务端复原，与实时流一致
  loadConversation() { getConversation(this.data.conversationId).then((messages) => { const hydrated = messages.map((m: any) => m.role === 'assistant' ? { ...hydrateAssistant(m), progress: '' } : m); this.setData({ messages: hydrated, lastMessageId: hydrated.length ? hydrated[hydrated.length - 1].id : '' }) }).catch(() => undefined) },
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
  chooseModel() { if (!this.data.sending) this.setData({ modelPickerVisible:true, modePickerVisible:false, pickerVisible:false }) },
  closeModelPicker() { if (!this.data.sending) this.setData({ modelPickerVisible:false }) },
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
  openKnowledgeDetail() { if (this.data.knowledgeId) wx.navigateTo({ url: `/pages/knowledge-detail/index?id=${this.data.knowledgeId}` }) },
  openDocument(e: any) {
    const id = String(e.currentTarget.dataset.id || '')
    if (!id) return
    // 行已左滑展开时，第一次点击先收起而不是打开文档
    if (this.getDirSwipe('doc', id) < 0) { this.applyDirSwipe('doc', id, 0); return }
    wx.navigateTo({ url: `/pages/document/index?id=${id}` })
  },
  openDiscover() { this.closeKnowledgePicker(); wx.navigateTo({ url: '/pages/market/index' }) },
  async openUpload() {
    if (!this.data.knowledgeId || this.data.sending) return
    this.setData({ uploadSheetVisible:true })
  },
  closeUploadSheet() {
    if (!this.data.sending) this.setData({ uploadSheetVisible:false })
  },
  async chooseUploadSource(e: any) {
    const source = String(e.currentTarget.dataset.source || '')
    if (!source || this.data.sending) return
    if (source === 'knowledge') {
      this.setData({ uploadSheetVisible:false })
      this.openKnowledgePicker()
      return
    }
    if (source === 'folder') {
      this.setData({ uploadSheetVisible:false })
      this.promptCreateFolder()
      return
    }
    if (source === 'article') {
      this.setData({ uploadSheetVisible:false, articleSheetVisible:true, articleUrl:'', articleImporting:false })
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
        wx.showToast({ title: error?.message || '上传失败，请稍后重试', icon: 'none' })
      }
    })
  },
  // 有文件夹时，上传/导入前让用户选择存到根目录还是某个文件夹
  pickTargetFolder(folders: Folder[], action: (folderId: string) => void) {
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
  closeArticleSheet() { if (!this.data.articleImporting) this.setData({ articleSheetVisible:false }) },
  async confirmArticleImport() {
    const url = this.data.articleUrl.trim()
    if (!url || this.data.articleImporting || !this.data.knowledgeId) return
    if (!/^https?:\/\/mp\.weixin\.qq\.com\//.test(url)) { wx.showToast({ title: '请粘贴公众号文章链接（mp.weixin.qq.com）', icon: 'none' }); return }
    this.pickTargetFolder(this.data.folders, async (folderId) => {
      this.setData({ articleImporting:true })
      try {
        const result = await importArticle(this.data.knowledgeId, url, folderId)
        this.setData({ articleSheetVisible:false, articleImporting:false, articleUrl:'' })
        wx.showToast({ title: result.status === 'completed' ? `已导入「${result.title}」` : '导入失败，请重试', icon: 'none', duration: 2500 })
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
    this.setData({ skillSheetVisible: true, modePickerVisible: false, pickerVisible: false, modelPickerVisible: false })
  },
  closeSheets() { this.setData({ skillSheetVisible: false }) },
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
    this.setData({ selectedSkillIds: ids, readyForInput: true }, () => this.syncCanSend())
    persistSkills(ids)
  },
  clearSkill() { this.applySkills([]) },
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
  // 历史对话抽屉：数据来自服务端 /api/conversations，按用户 + 知识库收窄，只显示根目录会话
  openHistory() {
    if (this.data.sending) return
    this.setData({ historyVisible: true, historyLoading: true })
    getConversations({ knowledgeId: this.data.knowledgeId, folderId: '' }).then((items) => {
      this.setData({ historyItems: items || [], historyLoading: false })
    }).catch(() => this.setData({ historyItems: [], historyLoading: false }))
  },
  closeHistory() { this.setData({ historyVisible: false }) },
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
    (this as any).userLeftConversation = false
    this.setData({ historyVisible: false, conversationId: id, conversationActive: true, messages: [], sending: false, canSend: false })
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
    this.setData({ input: '', conversationActive:true, sending: true, canSend: false, messages: [...this.data.messages, { id: userId, role: 'user', content, sources: [] }, assistantMessage(assistantId)], lastMessageId: assistantId })
    let assistant = ''
    const payload: any = { mode, conversation_id: this.data.conversationId || undefined, content, model: this.data.model, thinking: this.data.thinkingMode }
    // 选中的技能随请求下发（可多选），后端先加载并注入技能再执行任务
    if (skills.length) payload.skills = skills
    if (mode === 'knowledge') payload.knowledge_id = this.data.knowledgeId
    ;(this as any).cancelStream = streamChat(
      payload,
      (meta) => { this.setData({ conversationId: meta.conversation_id }); this.updateAssistant(assistantId, assistant, meta.sources as Source[]) },
      (delta) => { assistant += delta; this.flushDelta(assistantId, () => assistant) },
      () => { (this as any).cancelStream = null; this.flushReset(assistantId); this.updateAssistant(assistantId, assistant); this.settleAnswer(assistantId); this.setData({ sending: false }); this.syncCanSend() },
      (error) => { (this as any).cancelStream = null; this.flushReset(assistantId); this.updateAssistant(assistantId, assistant || '回答未完成，请稍后重新提问。'); this.settleAnswer(assistantId); this.setData({ sending: false }); this.syncCanSend(); wx.showToast({ title: error.message || error.errMsg || '回答失败', icon: 'none' }) },
      (label) => { if (label) this.updateAssistantProgress(assistantId, label) },
      (item) => this.pushTrace(assistantId, item),
      (artifact) => this.pushArtifact(assistantId, artifact),
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
  stopSend() {
    const cancel = (this as any).cancelStream
    if (cancel) { (this as any).cancelStream = null; cancel() }
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
  onUnload() {
    const cancel = (this as any).cancelStream; if (cancel) cancel()
    const flusher = (this as any).flusher; if (flusher) flusher.reset()
  },
  updateAssistant(id: string, content: string, sources?: Source[]) { const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, content, html: renderMarkdown(content), progress: content ? '' : message.progress, sources: sources ? decorateSources(sources) : message.sources } : message); this.setData({ messages, lastMessageId: id }) },
  updateAssistantProgress(id: string, progress: string) { const messages = this.data.messages.map((message: any) => message.id === id ? { ...message, progress } : message); this.setData({ messages }) },
  openSource(e: any) {
    const url = String(e.currentTarget.dataset.url || '')
    const id = e.currentTarget.dataset.id
    if (url) {
      wx.setClipboardData({ data: url, success: () => wx.showToast({ title: '网页链接已复制', icon: 'none' }) })
      return
    }
    if (id) wx.navigateTo({ url: `/pages/document/index?id=${id}` })
  },
})
