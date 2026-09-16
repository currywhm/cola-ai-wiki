import { EventStream } from './event-stream'

function appInstance() { return getApp<IAppOption>() }
function base() { return appInstance()?.globalData?.apiBase || 'http://127.0.0.1:8765' }
function token() { return appInstance()?.globalData?.token || wx.getStorageSync('llmwiki_token') || '' }

let authPromise: Promise<void> | null = null
function rawRequest<T>(path: string, method: 'GET' | 'POST' | 'PATCH' | 'DELETE' = 'GET', data?: any): Promise<T> {
  return new Promise((resolve, reject) => wx.request({ url: `${base()}${path}`, method: method as any, data, header: { Authorization: `Bearer ${token()}` }, success: (res) => { if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data as T); else reject(Object.assign(new Error((res.data as any)?.detail || `请求失败（${res.statusCode}）`), { statusCode: res.statusCode })) }, fail: reject }))
}
export function ensureAuth(): Promise<void> {
  if (token()) return Promise.resolve()
  if (wx.getStorageSync('llmwiki_logged_out')) return Promise.reject(Object.assign(new Error('你已退出登录，请重新使用微信登录'), { loggedOut: true }))
  if (authPromise) return authPromise
  authPromise = new Promise<void>((resolve, reject) => wx.login({ success: ({ code }) => rawRequest<{ token: string; user: any }>('/api/auth/login', 'POST', { code }).then((result) => { appInstance().globalData.token = result.token; appInstance().globalData.user = result.user; appInstance().globalData.loggedOut = false; wx.removeStorageSync('llmwiki_logged_out'); wx.setStorageSync('llmwiki_token', result.token); wx.setStorageSync('llmwiki_user', result.user); resolve() }).catch((error) => reject(error.statusCode === 401 ? Object.assign(error, { loggedOut: true }) : error)), fail: reject })).finally(() => { authPromise = null })
  return authPromise
}
export function resumeWechatLogin(): Promise<void> {
  wx.removeStorageSync('llmwiki_logged_out')
  appInstance().globalData.loggedOut = false
  return ensureAuth()
}
function request<T>(path: string, method: 'GET' | 'POST' | 'PATCH' | 'DELETE' = 'GET', data?: any): Promise<T> {
  return ensureAuth().then(() => {
    const attemptedToken = token()
    return rawRequest<T>(path, method, data).catch((error) => {
      if (error.statusCode !== 401) throw error
      if (token() === attemptedToken) {
        appInstance().globalData.token = ''
        wx.removeStorageSync('llmwiki_token')
        wx.removeStorageSync('llmwiki_user')
      }
      return ensureAuth().then(() => rawRequest<T>(path, method, data)).catch((authError) => {
        throw authError.statusCode === 401 ? Object.assign(authError, { loggedOut: true }) : authError
      })
    })
  })
}

export type Knowledge = { id: string; name: string; description: string; icon: string; document_count: number; updated_at: string }
export type Document = { id: string; filename: string; file_type: string; file_size: number; page_count: number; status: string; progress: number; error_message: string; organized_title?: string; summary?: string; tags?: string[]; key_points?: string[]; organize_status?: string; organize_method?: string }
export type Source = { id: string; document_id?: string; filename: string; page_number: number; quote: string; score: number; url?: string }

// 后端 harness 桥的过程节点：与 deepseek-harness 前端一致的过程展示数据
export type TraceKind = 'step' | 'reason' | 'tool' | 'skill' | 'agent' | 'note'
export type TraceState = 'run' | 'done' | 'error' | 'catalog' | 'load'
export type TraceItem = { type?: string; kind: TraceKind; state?: TraceState; title?: string; text?: string; detail?: string; name?: string; index?: number }
export async function login(code: string) { const result = await rawRequest<{ token: string; user: any }>('/api/auth/login', 'POST', { code }); appInstance().globalData.token = result.token; wx.setStorageSync('llmwiki_token', result.token); wx.setStorageSync('llmwiki_user', result.user); return result }
export const getMe = () => request<any>('/api/me')
export const updateMe = (data: { nickname: string; avatar: string }) => request<any>('/api/me', 'PATCH', data)
export const deleteAccount = () => request<any>('/api/me', 'DELETE')
export const getKnowledge = () => request<Knowledge[]>('/api/knowledge')
export type MarketKnowledge = { id: string; name: string; description: string; icon: string; category: string; subscribers: number; documents: number; updated_at: string }
export const getMarketKnowledge = (query = '', category = '') => request<MarketKnowledge[]>(`/api/market?query=${encodeURIComponent(query)}&category=${encodeURIComponent(category)}`)
export const getRecent = () => request<{ knowledge: any[]; items: any[] }>('/api/recent')
export const createKnowledge = (data: { name: string; description: string }) => request<Knowledge>('/api/knowledge', 'POST', data)
export const deleteKnowledge = (id: string) => request<any>(`/api/knowledge/${id}`, 'DELETE')
export const getKnowledgeDetail = (id: string) => request<{ knowledge: Knowledge; documents: Document[] }>(`/api/knowledge/${id}`)
export const getDocument = (id: string) => request<any>(`/api/documents/${id}`)
export const deleteDocument = (id: string) => request<any>(`/api/documents/${id}`, 'DELETE')
export const updateDocumentTags = (id: string, tags: string[]) => request<any>(`/api/documents/${id}/tags`, 'PATCH', { tags })
export const retryDocument = (id: string) => request<any>(`/api/documents/${id}/retry`, 'POST')
export const searchKnowledge = (q: string, knowledgeId?: string) => request<any[]>(`/api/search?q=${encodeURIComponent(q)}${knowledgeId ? `&knowledge_id=${knowledgeId}` : ''}`)
export type ModelOption = { id: string; value: string; name: string; short: string; badge: string }
export const getModels = () => request<ModelOption[]>('/api/models')
// 历史对话：服务端按 user_id + knowledge_id + folder_id 三层收窄。
// folderId 传空串 = 只看知识库根目录会话；不传 = 不限文件夹。
export const getConversations = (params: { knowledgeId?: string; folderId?: string; keyword?: string } = {}) => {
  const query: string[] = []
  if (params.knowledgeId) query.push(`knowledge_id=${encodeURIComponent(params.knowledgeId)}`)
  if (params.folderId !== undefined) query.push(`folder_id=${encodeURIComponent(params.folderId)}`)
  if (params.keyword) query.push(`q=${encodeURIComponent(params.keyword)}`)
  return request<any[]>(`/api/conversations${query.length ? `?${query.join('&')}` : ''}`)
}
export const getConversation = (id: string) => request<any[]>(`/api/conversations/${id}`)
export const deleteConversation = (id: string) => request<any>(`/api/conversations/${id}`, 'DELETE')
export const pinConversation = (id: string, pinned: boolean) => request<any>(`/api/conversations/${id}/pin`, 'POST', { pinned })
// 分享给微信好友：把一条回答落成一张分享卡片，好友点开 /pages/share/index?id=xxx 查看
export type ShareSourcePayload = { filename: string; page_number: number; url: string }
export type ShareCard = { id: string; question: string; answer: string; knowledge_name: string; sources: ShareSourcePayload[]; views: number; created_at: string }
export const createShare = (data: { question: string; answer: string; knowledge_name: string; sources: ShareSourcePayload[] }) => request<{ id: string }>('/api/shares', 'POST', data)
// 分享页对未登录的微信访客开放：不走登录态，也不用带 token
export const getShare = (id: string) => rawRequest<ShareCard>(`/api/shares/${encodeURIComponent(id)}`)
// 技能：技能广场（所有人可用）与我的技能（用户级隔离，仅作者本人可见可用）
export type Skill = {
  id: string; name: string; summary: string; prompt: string; icon: string
  developer_wechat: string; source: 'builtin' | 'custom'; builtin: boolean
  visibility: 'private' | 'public'; published: boolean; is_owner: boolean
  harness: string; use_count: number; like_count: number; favorite_count: number
  liked: boolean; favorited: boolean; created_at: string; updated_at: string
}
export type SkillForm = { name: string; summary: string; prompt: string; developer_wechat: string; icon: string }
export type SkillFlagResult = { id: string; active: boolean; count: number; field: string }
export const getSkills = (scope: 'market' | 'mine', query = '') => request<Skill[]>(`/api/skills?scope=${scope}&q=${encodeURIComponent(query)}`)
export const getSkill = (id: string) => request<Skill>(`/api/skills/${encodeURIComponent(id)}`)
export const createSkill = (data: SkillForm) => request<Skill>('/api/skills', 'POST', data)
export const updateSkill = (id: string, data: SkillForm) => request<Skill>(`/api/skills/${id}`, 'PATCH', data)
export const deleteSkill = (id: string) => request<any>(`/api/skills/${id}`, 'DELETE')
export const publishSkill = (id: string, published: boolean) => request<Skill>(`/api/skills/${id}/publish`, 'POST', { published })
export const likeSkill = (id: string, active: boolean) => request<SkillFlagResult>(`/api/skills/${id}/like`, 'POST', { active })
export const favoriteSkill = (id: string, active: boolean) => request<SkillFlagResult>(`/api/skills/${id}/favorite`, 'POST', { active })
export const logout = () => request<any>('/api/auth/logout', 'POST').catch(() => ({ ok: false }))
export type Folder = { id: string; name: string; document_count: number }
export const getFolders = (knowledgeId: string) => request<Folder[]>(`/api/knowledge/${knowledgeId}/folders`)
// 推荐问题：不传 folderId 时按整个知识库生成，传了则收窄到该文件夹
export const getSuggestions = (knowledgeId: string, folderId = '') => request<{ questions: string[] }>(`/api/knowledge/${knowledgeId}/suggestions${folderId ? `?folder_id=${encodeURIComponent(folderId)}` : ''}`)
export const createFolder = (knowledgeId: string, name: string) => request<Folder>(`/api/knowledge/${knowledgeId}/folders`, 'POST', { name })
export const deleteFolder = (folderId: string) => request<any>(`/api/folders/${folderId}`, 'DELETE')
export const moveDocument = (documentId: string, folderId: string) => request<any>(`/api/documents/${documentId}/move`, 'PATCH', { folder_id: folderId })
export const importArticle = (knowledgeId: string, url: string, folderId = '') => request<any>(`/api/knowledge/${knowledgeId}/import-article`, 'POST', { url, folder_id: folderId })
export type MembershipPlan = 'plus_monthly' | 'plus_quarterly' | 'plus_yearly' | 'pro_monthly' | 'pro_quarterly' | 'pro_yearly'
export const createPayOrder = (plan: MembershipPlan) => request<any>('/api/pay/orders', 'POST', { plan })
// 微信虚拟支付在 iOS 端要求微信客户端 >= 8.0.68，版本不足时直接拦截并提示升级
function ensureVirtualPaymentSupported(): void {
  const sys = wx.getSystemInfoSync()
  if (sys.platform !== 'ios') return
  const current = String(sys.version || '').split('.').map(Number)
  const minimum = [8, 0, 68]
  for (let i = 0; i < 3; i += 1) {
    if ((current[i] || 0) > minimum[i]) return
    if ((current[i] || 0) < minimum[i]) break
  }
  throw new Error('请将微信更新至最新版后再进行支付')
}
export async function payPlan(plan: MembershipPlan): Promise<void> {
  ensureVirtualPaymentSupported()
  const order = await createPayOrder(plan)
  const virtualPayment = (wx as any).requestVirtualPayment
  if (typeof virtualPayment !== 'function') throw new Error('当前微信版本不支持虚拟支付，请升级微信后重试')
  await new Promise<void>((resolve, reject) => virtualPayment({ ...order.payData, success: resolve, fail: reject }))
  // The client callback is only a UX signal. Delivery is confirmed by the server push/query path.
  for (let attempt = 0; attempt < 8; attempt += 1) {
    try {
      const state = await request<any>(`/api/pay/orders/${encodeURIComponent(order.out_trade_no)}`)
      if (state.deliver_status === 'delivered') return
    } catch { /* transient network failure; continue polling */ }
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  throw new Error('支付已提交，权益发放确认中，请稍后在“我的”页面查看')
}
export type UploadSource = 'file' | 'album' | 'camera'
type LocalUpload = { path: string; filename: string }

function pickerError(error: any, source: UploadSource): Error {
  const message = String(error?.errMsg || error?.message || '')
  if (message.includes('cancel')) return Object.assign(new Error('用户取消选择'), { cancelled: true })
  if (message.includes('not support') || message.includes('不支持')) {
    const label = source === 'file' ? '微信文件选择' : source === 'album' ? '相册导入' : '拍照扫描'
    return new Error(`当前开发环境不支持${label}，请使用真机预览或升级微信基础库`)
  }
  if (message.includes('auth deny') || message.includes('authorize')) {
    return new Error(source === 'camera' ? '需要相机权限才能扫描资料' : source === 'album' ? '需要相册权限才能导入图片' : '需要文件访问权限才能选择资料')
  }
  return new Error(message || '选择文件失败，请重试')
}

function timestampName(prefix: string) {
  const now = new Date()
  return `${prefix}_${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}_${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}${String(now.getSeconds()).padStart(2, '0')}.jpg`
}

function chooseLocalUpload(source: UploadSource): Promise<LocalUpload> {
  if (source === 'file') return new Promise((resolve, reject) => wx.chooseMessageFile({
    count: 1, type: 'file', extension: ['pdf', 'docx', 'txt', 'md', 'markdown', 'csv', 'jpg', 'jpeg', 'png', 'webp'],
    success: pick => { const item = pick.tempFiles[0]; if (!item) { reject(new Error('没有选择文件')); return }; resolve({ path: item.path, filename: item.name || '未命名文件' }) }, fail: reject,
  })).catch(error => Promise.reject(pickerError(error, source))) as Promise<LocalUpload>
  const sourceType: ('album' | 'camera')[] = source === 'camera' ? ['camera'] : ['album']
  const mediaApi = wx as any
  if (typeof mediaApi.chooseMedia === 'function') return new Promise((resolve, reject) => mediaApi.chooseMedia({
    count: 1, mediaType: ['image'], sourceType,
    success: (pick: any) => { const item = pick.tempFiles?.[0]; if (!item?.tempFilePath) { reject(new Error('没有选择图片')); return }; resolve({ path: item.tempFilePath, filename: timestampName(source === 'camera' ? '扫描' : '图片') }) },
    fail: (error: any) => reject(pickerError(error, source)),
  }))
  return new Promise((resolve, reject) => wx.chooseImage({
    count: 1, sizeType: ['original'], sourceType,
    success: pick => { const path = pick.tempFilePaths[0]; if (!path) { reject(new Error('没有选择图片')); return }; resolve({ path, filename: timestampName(source === 'camera' ? '扫描' : '图片') }) }, fail: error => reject(pickerError(error, source)),
  }))
}

function selectUploadSource(): Promise<UploadSource> {
  return new Promise((resolve, reject) => wx.showActionSheet({
    itemList: ['从微信文件选择', '从相册导入图片', '拍照扫描文字'],
    success: ({ tapIndex }) => resolve((['file', 'album', 'camera'] as UploadSource[])[tapIndex]), fail: reject,
  }))
}

function uploadPickedFile(knowledgeId: string, file: LocalUpload, folderId = ''): Promise<any> {
  return ensureAuth().then(() => new Promise((resolve, reject) => {
    wx.showLoading({ title: '正在上传', mask: true })
    let loadingVisible = true
    const finish = () => { if (loadingVisible) { wx.hideLoading(); loadingVisible = false } }
    wx.uploadFile({
      url: `${base()}/api/knowledge/${knowledgeId}/documents`, filePath: file.path, name: 'file',
      formData: { folder_id: folderId },
      header: { Authorization: `Bearer ${token()}`, 'X-Upload-Filename': encodeURIComponent(file.filename) },
      success: res => { finish(); try { const body = JSON.parse(res.data); if (res.statusCode >= 200 && res.statusCode < 300) resolve(body); else reject(Object.assign(new Error(body?.detail || '上传失败'), { statusCode: res.statusCode })) } catch { reject(new Error('上传响应无效，请重试')) } },
      fail: error => { finish(); reject(error) },
    })
  }))
}

export function uploadLocalFile(knowledgeId: string, path: string, filename: string, folderId = ''): Promise<any> {
  return uploadPickedFile(knowledgeId, { path, filename }, folderId)
}

export async function uploadDocument(knowledgeId: string, source?: UploadSource, folderId = ''): Promise<any> {
  const selected = source || await selectUploadSource()
  const file = await chooseLocalUpload(selected)
  return uploadPickedFile(knowledgeId, file, folderId)
}
export function streamChat(payload: { knowledge_id?: string; conversation_id?: string; folder_id?: string; content: string; model?: string; mode?: 'knowledge' | 'web'; thinking?: 'quick' | 'deep'; skill?: string }, onMeta: (value: any) => void, onDelta: (value: string) => void, onDone: () => void, onError: (error: any) => void, onProgress?: (label: string) => void, onTrace?: (item: TraceItem) => void) {
  let task: any; let finished = false; let receivedChunk = false
  const fail = (error: any) => { if (!finished) { finished = true; onError(error) } }
  const parser = new EventStream((data) => { if (finished) return; if (data.type === 'meta') onMeta(data); else if (data.type === 'delta') onDelta(data.content || ''); else if (data.type === 'progress') { if (onProgress) onProgress(data.label || '') } else if (data.type === 'trace') { const item = data as TraceItem; if (onTrace) onTrace(item); else if (onProgress) { if (item.kind === 'tool' || item.kind === 'skill' || item.kind === 'agent') { if (item.state === 'run' || item.state === 'load') onProgress(`${item.title || '处理中'}…`) } else if (item.kind === 'step') onProgress('正在思考…'); else onProgress(item.title || '') } } else if (data.type === 'error') fail(new Error(data.message || '回答未完成')); else if (data.type === 'done') { finished = true; onDone() } })
  ensureAuth().then(() => { if (finished) return; task = wx.request({ url: `${base()}/api/chat/stream`, method: 'POST', enableChunked: true, responseType: 'text', data: payload, timeout: 300000, header: { 'content-type': 'application/json', Authorization: `Bearer ${token()}` }, success: (res: any) => { if (finished) return; if (res.statusCode < 200 || res.statusCode >= 300) { let detail = ''; try { detail = (typeof res.data === 'string' ? JSON.parse(res.data) : res.data)?.detail || '' } catch { /* 非 JSON 错误体 */ } fail(new Error(detail || `回答请求失败（${res.statusCode}）`)); return }; try { if (!receivedChunk && typeof res.data === 'string') parser.push(res.data); if (!finished) fail(new Error('连接已中断，回答未完成，请重新提问。')) } catch { fail(new Error('回答数据格式异常，请重试。')) } }, fail } as any); task.onChunkReceived((chunk: any) => { if (finished) return; receivedChunk = true; try { parser.push(chunk.data) } catch { fail(new Error('回答数据格式异常，请重试。')) } }) }).catch(fail)
  return () => { finished = true; if (task) task.abort() }
}
