import { ensureAuth } from './services/api'

// 按小程序运行环境切换后端地址：开发版连本地，体验版/正式版连生产域名（上线前替换为真实 HTTPS 域名）
const API_BASE_BY_ENV: Record<string, string> = {
  develop: 'http://127.0.0.1:8765',
  trial: 'https://api.cola-wiki.example.com',
  release: 'https://api.cola-wiki.example.com',
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
  globalData: { apiBase: resolveApiBase(), token: '', user: wx.getStorageSync('llmwiki_user') || null, loggedOut: !!wx.getStorageSync('llmwiki_logged_out'), pendingImportFile: null as { path: string; filename: string } | null },
  onLaunch(options?: any) {
    this.captureOpenFile?.(options)
    const cachedToken = wx.getStorageSync('llmwiki_token')
    if (cachedToken) this.globalData.token = cachedToken
    this.globalData.user = wx.getStorageSync('llmwiki_user') || null
    this.globalData.loggedOut = !!wx.getStorageSync('llmwiki_logged_out')
    const startLogin = () => ensureAuth().catch((error) => console.warn('微信登录待配置:', error))
    if (this.globalData.loggedOut) return
    const privacyApi = wx as any
    if (typeof privacyApi.getPrivacySetting !== 'function') { startLogin(); return }
    privacyApi.getPrivacySetting({ success: (result: any) => { if (result.needAuthorization && typeof privacyApi.requirePrivacyAuthorize === 'function') privacyApi.requirePrivacyAuthorize({ success: startLogin, fail: (error: any) => console.warn('隐私授权未完成:', error) }); else startLogin() }, fail: startLogin })
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
