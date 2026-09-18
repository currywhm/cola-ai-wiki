// 按小程序运行环境切换后端地址：开发版连本地，体验版/正式版连生产域名（上线前替换为真实 HTTPS 域名）
const API_BASE_BY_ENV: Record<string, string> = {
  develop: 'http://127.0.0.1:8765',
  trial: 'https://airouter-api.zeabur.app',
  release: 'https://airouter-api.zeabur.app',
}
function resolveApiBase(): string {
  try {
    const env = wx.getAccountInfoSync()?.miniProgram?.envVersion || 'develop'
    return API_BASE_BY_ENV[env] || API_BASE_BY_ENV.develop
  } catch {
    return API_BASE_BY_ENV.develop
  }
}

App<IAppOption>({
  globalData: { apiBase: resolveApiBase(), token: '', user: wx.getStorageSync('llmwiki_user') || null, loggedOut: !!wx.getStorageSync('llmwiki_logged_out'), pendingImportFile: null as { path: string; filename: string } | null, splashShown: false },
  onLaunch(options?: any) {
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
