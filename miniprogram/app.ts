import { initCloud } from './services/cloud'

App<IAppOption>({
  globalData: { token: '', user: wx.getStorageSync('llmwiki_user') || null, loggedOut: !!wx.getStorageSync('llmwiki_logged_out'), pendingImportFile: null as { path: string; filename: string } | null, splashShown: false },
  onLaunch(options?: any) {
    // 原生限制：发起 callContainer 之前必须全局初始化一次云托管环境（见 services/cloud.ts）
    initCloud()
    this.captureOpenFile?.(options)
    const cachedToken = wx.getStorageSync('llmwiki_token')
    if (cachedToken) this.globalData.token = cachedToken
    this.globalData.user = wx.getStorageSync('llmwiki_user') || null
    this.globalData.loggedOut = !!wx.getStorageSync('llmwiki_logged_out')
    // 微信登录必须由登录页中的用户操作触发；各业务入口与接口层负责游客拦截。
  },
  onShow(options?: any) {
    this.captureOpenFile?.(options)
  },
  // 微信「用小程序打开」进入时，客户端在 query 中携带 filePath/fileName，暂存后由首页弹出知识库选择
  captureOpenFile(options?: any) {
    const query = options?.query || {}
    const filePath = query.filePath || query.file_path || ''
    if (!filePath) return
    const rawName = query.fileName || query.file_name || ''
    let filename = '微信文件'
    try { filename = decodeURIComponent(rawName) || '微信文件' } catch { filename = rawName || '微信文件' }
    this.globalData.pendingImportFile = { path: String(filePath), filename }
  },
})
