import { ensurePrivacyAuthorized, isPrivacyScopeError } from './privacy'
import { PREVIEW_FILE_TYPES } from '../utils/file-type'
import { callContainer } from './cloud'

function appInstance() { return getApp<IAppOption>() }
function token() { return appInstance()?.globalData?.token || wx.getStorageSync('llmwiki_token') || '' }

// 页面展示与业务请求要分开判断：游客仍可切换 tab、浏览入口，
// 真正使用资料、问答或账号能力时再由 goLogin 统一进入登录页。
export function isLoggedIn(): boolean { return !!token() }
export function goLogin(): void { redirectToLogin() }

// 协议同意版本：合规文档（content/legal）改动后要同步升这里，否则老用户不会重新看到更新后的协议。
// 与 server/content/legal/manifest.json 的 version 一一对应。
export const AGREEMENT_VERSION = '2026-09-19.1'

const AGREEMENT_KEY = 'llmwiki_agreements'

export function hasAcceptedAgreements(): boolean {
  const accepted = wx.getStorageSync(AGREEMENT_KEY)
  return !!accepted && accepted.version === AGREEMENT_VERSION
}

export function markAgreementsAccepted(version = AGREEMENT_VERSION): void {
  wx.setStorageSync(AGREEMENT_KEY, { version, acceptedAt: Date.now() })
}

export function clearAuthSession(): void {
  const app = appInstance()
  app.globalData.token = ''
  app.globalData.user = null
  app.globalData.loggedOut = true
  wx.removeStorageSync('llmwiki_token')
  wx.removeStorageSync('llmwiki_user')
  wx.setStorageSync('llmwiki_logged_out', true)
}

function redirectToLogin(): void {
  try {
    const pages = getCurrentPages()
    const route = pages.length ? (pages[pages.length - 1] as any).route : ''
    if (route === 'pages/login/index') return
  } catch { /* getCurrentPages 在极少数启动阶段不可用，继续跳登录页 */ }
  wx.reLaunch({ url: '/pages/login/index' })
}


let authPromise: Promise<void> | null = null
/** 错误文案：后端版本落后时 FastAPI 只回英文 detail（Not Found / Method Not Allowed），
 *  这种「接口不存在」要翻译成人话，否则用户看到的就是一个干巴巴的 Not Found。 */
function readableError(statusCode: number, data: any): string {
  const detail = String((data && data.detail) || '').trim()
  if (statusCode === 404 && (!detail || detail === 'Not Found')) return '服务端还没有这个接口（版本过旧），请重新部署后端后再试'
  if (statusCode === 405) return '服务端版本过旧，接口不匹配，请重新部署后端后再试'
  return detail || `请求失败（${statusCode}）`
}

// 所有业务请求都走微信云托管私有协议：不需要配置服务器域名，也不经过公网。
// 返回体（statusCode + data）与 wx.request 一致，所以错误映射与 401 重试逻辑照旧。
function rawRequest<T>(path: string, method: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE' = 'GET', data?: any): Promise<T> {
  return callContainer<T>({ path, method, data, header: { Authorization: `Bearer ${token()}` } }).then((res) => {
    if (res.statusCode >= 200 && res.statusCode < 300) return res.data as T
    throw Object.assign(new Error(readableError(res.statusCode, res.data)), { statusCode: res.statusCode })
  })
}

// 长任务：容器通道单次调用上限 15s，可能更久的动作都由后端排队，
// 前端拿 job_id 轮询。（对话流见 followChatRun）
export type JobView = { id: string; kind: string; status: 'queued' | 'running' | 'done' | 'error' | 'interrupted'; progress: string; result: any; error: string }

export function pollJob(jobId: string, handlers: { onProgress?: (label: string) => void; onDone: (result: any) => void; onError: (error: any) => void }, interval = 1500): () => void {
  let finished = false
  let timer: any = null
  let failures = 0
  let lastProgress = ''
  const stop = () => { finished = true; if (timer) { clearTimeout(timer); timer = null } }
  const tick = () => {
    if (finished) return
    request<JobView>(`/api/jobs/${jobId}`).then((job) => {
      if (finished) return
      failures = 0
      if (job.progress && job.progress !== lastProgress) {
        lastProgress = job.progress
        if (handlers.onProgress) handlers.onProgress(job.progress)
      }
      if (job.status === 'done') { stop(); handlers.onDone(job.result || {}); return }
      if (job.status === 'error' || job.status === 'interrupted') { stop(); handlers.onError(new Error(job.error || '任务未完成，请重试')); return }
      timer = setTimeout(tick, interval)
    }).catch((error) => {
      if (finished) return
      failures += 1
      // 网络抖动不该让任务看起来失败：退避重试几次再报错
      if (failures >= 5) { stop(); handlers.onError(error); return }
      timer = setTimeout(tick, interval * failures)
    })
  }
  tick()
  return stop
}

// 二进制资源：后端路径 → 对象存储预签名地址（直接 wx.downloadFile）
function downloadToTemp(url: string, timeout = 120000): Promise<string> {
  return new Promise((resolve, reject) => {
    wx.downloadFile({
      url, timeout,
      success: (res: any) => {
        if (res.statusCode === 200 && res.tempFilePath) { resolve(res.tempFilePath); return }
        reject(new Error(`文件下载失败（${res.statusCode}）`))
      },
      fail: () => reject(new Error('文件下载失败，请检查网络后重试')),
    })
  })
}

export type ResolvedAsset = { mode?: 'cos' | 'inline' | 'local'; url?: string; base64?: string; mime?: string; filename?: string; error?: string }

/** 批量解析后端资源路径：一次来回换回可下载地址或小图字节。 */
export function resolveAssets(paths: string[]): Promise<Record<string, ResolvedAsset>> {
  if (!paths.length) return Promise.resolve({})
  return rawRequest<{ items: Record<string, ResolvedAsset> }>('/api/assets/resolve', 'POST', { paths: paths.slice(0, 32) }).then((res) => res.items || {})
}

/** 单条资源 → 可交给 wx.downloadFile 的地址。 */
export function assetUrl(path: string): Promise<string> {
  return resolveAssets([path]).then((items) => {
    const item = items[path]
    if (!item || item.error || !item.url) throw new Error((item && item.error) || '文件暂时无法下载，请稍后重试')
    return item.url
  })
}

/** 后端排队执行的接口：拿到 job_id 就等任务结果，否则原样返回。
 *  onProgress 透传后端写进 jobs.progress 的那句话（「正在审阅…」），调用方可以显示真实进度。 */
function jobResult<T>(result: any, onProgress?: (label: string) => void): Promise<T> {
  if (!result || !result.pending || !result.job_id) return Promise.resolve(result as T)
  return new Promise<T>((resolve, reject) => {
    pollJob(result.job_id, { onProgress, onDone: (done) => resolve(done as T), onError: reject })
  })
}

// 微信登录只允许从登录页的用户明确操作触发。其他页面没有令牌时只负责回到登录页，
// 不在这里静默调用 wx.login，避免绕过登录页直接进入问 AI 首页。
export function ensureAuth(): Promise<void> {
  if (token()) return Promise.resolve()
  redirectToLogin()
  return Promise.reject(Object.assign(new Error('请先使用微信登录'), { needsLogin: true, loggedOut: !!wx.getStorageSync('llmwiki_logged_out') }))
}

function performWechatLogin(): Promise<void> {
  if (token()) return Promise.resolve()
  if (authPromise) return authPromise
  authPromise = new Promise<void>((resolve, reject) => wx.login({ success: ({ code }) => rawRequest<{ token: string; user: any }>('/api/auth/login', 'POST', { code }).then((result) => { appInstance().globalData.token = result.token; appInstance().globalData.user = result.user; appInstance().globalData.loggedOut = false; wx.removeStorageSync('llmwiki_logged_out'); wx.setStorageSync('llmwiki_token', result.token); wx.setStorageSync('llmwiki_user', result.user); resolve() }).catch((error) => reject(error.statusCode === 401 ? Object.assign(error, { loggedOut: true }) : error)), fail: reject })).finally(() => { authPromise = null })
  return authPromise
}

export function resumeWechatLogin(): Promise<void> {
  wx.removeStorageSync('llmwiki_logged_out')
  appInstance().globalData.loggedOut = false
  return performWechatLogin()
}
function request<T>(path: string, method: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE' = 'GET', data?: any): Promise<T> {
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

export type ArtifactKind = 'image' | 'document' | 'text' | 'file'

// 工具产物：harness agent 本轮生成的文件。后端收好之后随消息下发，重进对话也会回放；
// 前端只负责展示与打开——收集、存盘、按用户收窄全部在后端。
export type ArtifactView = { id: string; name: string; type: string; suffix: string; kind: ArtifactKind; size: number; size_label: string; mime: string; document_id: string; saved: boolean; note: string; created_at: string; url: string; text?: string; truncated?: boolean }

/** 站内预览：文本类产物直接拿正文（Markdown / 纯文本 / 代码），其它类型只看类型信息 */
export const getArtifactPreview = (id: string) => request<ArtifactView>(`/api/artifacts/${id}/preview`)

/**
 * 把产物下到本地临时文件。
 * 微信的原生预览（图片 / 文档）与「转发到聊天」都只认本地路径，所以这一步不能省。
 */
export function downloadArtifact(artifactId: string): Promise<string> {
  return ensureAuth()
    .then(() => assetUrl(`/api/artifacts/${artifactId}/download`))
    .then((url) => downloadToTemp(url))
}

// shared/read_only：好友共享过来的只读镜像；source_name 是来源库名称；source_missing 表示来源已被删掉
export type Knowledge = { id: string; name: string; description: string; icon: string; avatar?: string; document_count: number; category?: string; subscribers?: number; published?: boolean; published_at?: string; publishable?: boolean; updated_at: string; shared?: boolean; subscribed?: boolean; subscription_source?: string; read_only?: boolean; source_name?: string; source_missing?: boolean; inbox?: boolean; shareable?: boolean }
export type Document = { id: string; filename: string; file_type: string; file_size: number; page_count: number; status: string; progress: number; error_message: string; organized_title?: string; summary?: string; tags?: string[]; key_points?: string[]; organize_status?: string; organize_method?: string }
export type Source = { id: string; document_id?: string; filename: string; page_number: number; quote: string; score: number; url?: string }

// 后端 harness 桥的过程节点：与 deepseek-harness 前端一致的过程展示数据
export type TraceKind = 'step' | 'reason' | 'tool' | 'skill' | 'agent' | 'note' | 'plan'
export type TraceState = 'run' | 'done' | 'error' | 'catalog' | 'load'
export type TraceItem = {
  type?: string
  kind: TraceKind
  state?: TraceState
  title?: string
  text?: string
  detail?: string
  name?: string
  index?: number
  /* 工具调用：call_id 用来合并同一调用的 run/done，并隔离并行同名工具。 */
  call_id?: string
  tool_variant?: string
  tool_summary?: string
  tool_input?: string
  tool_meta?: any
  tool_output?: string
  tool_error?: string
  tool_result_summary?: string
  tool_result_meta?: any
  duration_ms?: number
  /* 计划模式：kind==='plan' 时携带完整计划 markdown 与评审 id（= 该轮 harness 会话 id） */
  plan?: string
  review_id?: string
}
export async function login(code: string) { const result = await rawRequest<{ token: string; user: any }>('/api/auth/login', 'POST', { code }); appInstance().globalData.token = result.token; wx.setStorageSync('llmwiki_token', result.token); wx.setStorageSync('llmwiki_user', result.user); return result }
export const getMe = () => request<any>('/api/me')
// 用户偏好（目前用于持久化「已选技能」）：skills 为 null 表示服务端从未设置过
export type UserPreferences = { skills: string[] | null }
export const getPreferences = () => request<UserPreferences>('/api/me/preferences')
export const savePreferences = (data: { skills?: string[] }) => request<UserPreferences>('/api/me/preferences', 'PUT', data)
// 运营文案（使用技巧）：正文与配图都在后端，改文件即生效，前端只负责渲染。
export type TipEntry = { id: string; title: string; summary: string; icon: string; cover: string; read_minutes: number }
export type TipGroup = { id: string; title: string; entries: TipEntry[] }
export type TipIndex = { version: string; title: string; subtitle: string; intro: string; updated_at: string; groups: TipGroup[] }
export type TipRun = { text: string; style: 'plain' | 'strong' | 'code' }
export type TipBlock = { type: 'heading' | 'paragraph' | 'bullet' | 'step' | 'note' | 'figure'; level?: number; index?: number; text?: string; runs?: TipRun[]; src?: string; caption?: string }
export type TipDetail = TipEntry & { group: string; blocks: TipBlock[] }
// 后端下发的配图是相对路径（/api/content/assets/...）。渲染层不能带登录态、也没有域名可用，
// 所以统一交给 services/media.ts：它按路径换回可渲染的本地文件。
export const contentAssetUrl = (path: string) => (path || '')

export const getTipsIndex = () => rawRequest<TipIndex>('/api/content/tips')
export const getTipDetail = (id: string) => rawRequest<TipDetail>(`/api/content/tips/${id}`)
// 合规文档（关于 / 数据管理 / 隐私安全 / 隐私保护指引 / 服务协议 / 软件许可 /
// 会员服务条款）：正文与更新日期都在后端，改文案不用重新提交审核，
// 而且走匿名请求——审核和未登录状态都要能直接打开。
export type LegalDocMeta = { id: string; title: string; summary: string; updated_at: string; effective_at: string }
export type LegalIndex = { version: string; title: string; subtitle: string; updated_at: string; docs: LegalDocMeta[] }
export type LegalDetail = LegalDocMeta & { blocks: TipBlock[] }
export const getLegalIndex = () => rawRequest<LegalIndex>('/api/content/legal')
export const getLegalDoc = (id: string) => rawRequest<LegalDetail>(`/api/content/legal/${id}`)
// 两个字段都可选：只改昵称时不会顺手把头像清掉（后端同样按“未传则保持原值”处理）
export const updateMe = (data: { nickname?: string; avatar?: string }) => request<any>('/api/me', 'PATCH', data)

// 文件不能走容器通道（请求体上限 100KiB）：先向服务端要一份只允许写单个对象、
// 且锁死体积上限的直传凭证，再由客户端直接写进对象存储，最后回执登记。
type UploadSlot = { mode: string; url?: string; fields?: Record<string, string>; key?: string; document_id?: string; filename?: string; max_bytes?: number; suffix?: string }

function suffixOfPath(path: string): string {
  const hit = /\.([A-Za-z0-9]+)(?:[?#]|$)/.exec(String(path || ''))
  return hit ? `.${hit[1].toLowerCase()}` : ''
}

async function acquireUploadSlot(kind: 'document' | 'avatar' | 'knowledge-avatar', filename: string, extra: Record<string, any> = {}): Promise<UploadSlot> {
  const slot = await request<UploadSlot>('/api/uploads/direct', 'POST', { kind, filename, ...extra })
  if (slot.mode !== 'cos' || !slot.url || !slot.key) throw new Error('当前环境不支持文件直传，请稍后重试')
  return slot
}

function uploadToCos(filePath: string, slot: UploadSlot): Promise<void> {
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: String(slot.url), filePath, name: 'file', formData: slot.fields || {},
      success: (res: any) => {
        const status = Number(res.statusCode || 0)
        if (status >= 200 && status < 300) { resolve(); return }
        reject(new Error(`文件上传失败（${status}）`))
      },
      fail: () => reject(new Error('文件上传失败，请检查网络后重试')),
    })
  })
}

// 微信「头像昵称填写能力」回调给的是本机临时路径（http://tmp/... 或 wxfile://...），
// 换设备或重装就失效，所以选完要立刻传到对象存储持久化，再回执写回用户资料。
export function uploadAvatar(filePath: string): Promise<{ avatar: string }> {
  return ensureAuth().then(async () => {
    const slot = await acquireUploadSlot('avatar', filePath, { suffix: suffixOfPath(filePath) })
    await uploadToCos(filePath, slot)
    return request<{ avatar: string }>('/api/me/avatar/complete', 'POST', { key: slot.key })
  })
}

export function uploadKnowledgeAvatar(knowledgeId: string, filePath: string): Promise<{ avatar: string }> {
  return ensureAuth().then(async () => {
    const slot = await acquireUploadSlot('knowledge-avatar', filePath, { knowledge_id: knowledgeId, suffix: suffixOfPath(filePath) })
    await uploadToCos(filePath, slot)
    return request<{ avatar: string }>(`/api/knowledge/${knowledgeId}/avatar/complete`, 'POST', { key: slot.key })
  })
}

// 后端存的是相对地址（/api/avatars/...），渲染前由 services/media.ts 解析成可取用的地址
export const userAvatarUrl = (avatar?: string) => (avatar || '')


export const deleteAccount = () => request<any>('/api/me', 'DELETE')
export const getKnowledge = () => request<Knowledge[]>('/api/knowledge')
export type MarketKnowledge = { id: string; name: string; description: string; icon: string; avatar?: string; category: string; subscribers: number; documents: number; updated_at: string; published_at?: string; publisher_name: string; publisher_avatar?: string; subscribed: boolean; owned: boolean }
export const getMarketKnowledge = (query = '', category = '') => request<MarketKnowledge[]>(`/api/market?query=${encodeURIComponent(query)}&category=${encodeURIComponent(category)}`)
export type SubscriptionResult = { knowledge_id: string; name: string; documents: number; subscribers: number; already: boolean; message: string }
export const subscribeKnowledge = (knowledgeId: string) => request<SubscriptionResult>('/api/subscriptions', 'POST', { knowledge_id: knowledgeId })
export const unsubscribeKnowledge = (knowledgeId: string) => request<{ ok: boolean; removed: boolean; subscribers: number }>(`/api/subscriptions/${knowledgeId}`, 'DELETE')
// 最近知识只展示最新的 20 条（服务端同样按 20 条收敛，不白拉数据）
export const getRecent = (limit = 20) => request<{ knowledge: any[]; items: any[] }>(`/api/recent?limit=${limit}`)
export const createKnowledge = (data: { name: string; description: string }) => request<Knowledge>('/api/knowledge', 'POST', data)
// 上架要通读整个知识库做合规检查（最多 80 份资料），可能超过容器通道单次调用上限：
// 后端排队执行，这里等任务结果；下架是瞬时动作，后端直接返回。
export function publishKnowledge(id: string, published: boolean, acknowledged = false, onProgress?: (label: string) => void): Promise<{ ok: boolean; published: boolean; category?: string; subscribers?: number; reviewed_documents?: number; message: string }> {
  return request<any>(`/api/knowledge/${id}/publish`, 'POST', { published, acknowledged })
    .then((result) => jobResult<{ ok: boolean; published: boolean; category?: string; subscribers?: number; reviewed_documents?: number; message: string }>(result, onProgress))
}


export const deleteKnowledge = (id: string) => request<any>(`/api/knowledge/${id}`, 'DELETE')
export const getKnowledgeDetail = (id: string) => request<{ knowledge: Knowledge; documents: Document[] }>(`/api/knowledge/${id}`)
export const getDocument = (id: string) => request<any>(`/api/documents/${id}`)


/**
 * 原文预览：先把原文件下到本地临时文件，再交给微信内置文档渲染器打开。
 * 这是微信生态里唯一能 1:1 还原 PDF / Word / Excel / PPT 版式的路径；
 * 站内阅读只负责展示供检索用的正文，不看版式。
 */
export function previewDocument(id: string, fileType: string): Promise<void> {
  const type = String(fileType || '').replace(/^\./, '').toLowerCase()
  if (PREVIEW_FILE_TYPES.indexOf(type) < 0) return Promise.reject(new Error('该格式暂不支持原文预览'))
  wx.showLoading({ title: '正在打开原文', mask: true })
  const done = () => wx.hideLoading()
  return ensureAuth()
    .then(() => assetUrl(`/api/documents/${id}/download`))
    .then((url) => downloadToTemp(url))
    .then((filePath) => new Promise<void>((resolve, reject) => {
      // 临时文件路径通常不带扩展名，必须显式告诉渲染器按哪种格式打开，否则会报格式不支持
      wx.openDocument({
        filePath,
        fileType: type as any,
        showMenu: true,
        success: () => { done(); resolve() },
        fail: (error: any) => { done(); reject(new Error((error && error.errMsg) || '该文件暂时无法打开')) },
      })
    }))
    .catch((error) => { done(); throw error })
}

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
// 分享给微信好友：把一条回答落成一张分享卡片，好友点开 /package-features/pages/share/index?id=xxx 查看
// 知识库邀请：把整个资料库分享给微信好友。一条链接只认第一个接受的好友，
// 领取后对方在自己的「共享知识库」里得到一份只读镜像（来源更新会自动同步）。
export type KnowledgeShareLink = { token: string; path: string; name: string; expires_at: string; days: number }
export type KnowledgeShareView = { state: 'open' | 'mine' | 'taken' | 'expired' | 'revoked' | 'missing'; available: boolean; name: string; description: string; document_count: number; expires_at: string; joined_knowledge_id: string }
export const createKnowledgeShare = (knowledgeId: string) => request<KnowledgeShareLink>(`/api/knowledge/${encodeURIComponent(knowledgeId)}/share`, 'POST', {})
// 分享页对未登录的微信访客开放：不带 token 拿概览，带了就顺带告诉我这条是不是我领的
export const getKnowledgeShare = (token: string) => rawRequest<KnowledgeShareView>(`/api/knowledge-shares/${encodeURIComponent(token)}`)
export type KnowledgeAcceptResult = { knowledge_id: string; name: string; documents: number; already: boolean; read_only: boolean; message: string }
export const acceptKnowledgeShare = (token: string) => request<KnowledgeAcceptResult>(`/api/knowledge-shares/${encodeURIComponent(token)}/accept`, 'POST', {})
export const revokeKnowledgeShare = (token: string) => request<{ ok: boolean; removed: number }>(`/api/knowledge-shares/${encodeURIComponent(token)}/revoke`, 'POST', {})
export type ShareSourcePayload = { filename: string; page_number: number; url: string }
export type ShareFilePayload = { artifact_id: string; name: string }
export type ShareFileView = { index: number; name: string; size: number; suffix: string }
export type ShareCard = { id: string; title: string; question: string; answer: string; knowledge_name: string; sources: ShareSourcePayload[]; files: ShareFileView[]; views: number; created_at: string }
export const createShare = (data: { title?: string; question: string; answer: string; knowledge_name: string; sources: ShareSourcePayload[]; files?: ShareFilePayload[] }) => request<{ id: string }>('/api/shares', 'POST', data)
// 分享页对未登录的微信访客开放：不走登录态，也不用带 token
export const getShare = (id: string) => rawRequest<ShareCard>(`/api/shares/${encodeURIComponent(id)}`)
// 收进自己的「共享知识库」：这一步要登录（小程序里就是微信登录），所以要带 token
export type ShareClaimResult = { knowledge_id: string; knowledge_name: string; folder_id: string; documents: any[]; skipped: string[]; already: boolean; message: string }
// 收进「共享知识库」要复制文件并抽取正文，后端排队执行，这里等任务结果
export function claimShare(id: string, onProgress?: (label: string) => void): Promise<ShareClaimResult> {
  return request<any>(`/api/shares/${encodeURIComponent(id)}/claim`, 'POST', {}).then((result) => jobResult<ShareClaimResult>(result, onProgress))
}

// 分享页里的文件：公开只读，凭分享 id + 序号取件，不需要登录态
export const shareFilePath = (id: string, index: number) => `/api/shares/${encodeURIComponent(id)}/files/${index}`
/** 把对话里勾选的内容与文件存进指定知识库（分享面板的「存到知识库」）。
 *  文件要逐个抽取正文，后端排队执行，这里等任务结果。 */
export function saveSelectionToKnowledge(data: { knowledge_id: string; folder_id?: string; title: string; content: string; artifact_ids: string[] }, onProgress?: (label: string) => void): Promise<{ knowledge_id: string; knowledge_name: string; saved: any[]; skipped: string[]; message: string }> {
  return request<any>('/api/shares/to-knowledge', 'POST', data)
    .then((result) => jobResult<{ knowledge_id: string; knowledge_name: string; saved: any[]; skipped: string[]; message: string }>(result, onProgress))
}

// 技能：技能广场（所有人可用）与我的技能（用户级隔离，仅作者本人可见可用）
export type Skill = {
  id: string; name: string; summary: string; prompt: string; icon: string
  developer_wechat: string; source: 'builtin' | 'custom'; builtin: boolean
  visibility: 'private' | 'public'; published: boolean; is_owner: boolean
  harness: string; use_count: number; like_count: number; favorite_count: number
  liked: boolean; favorited: boolean; created_at: string; updated_at: string
  // 技能包型技能（由 harness 的 skill-creator 生成）会带出包内文件清单；
  // degraded=true 表示模型没写出完整技能包，后端按用户填写的内容兜底合成了一份。
  files?: string[]; package?: boolean; degraded?: boolean
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
// 增强提示词：把随手写的一句话按技能模板改写成可执行的技能指令。
// 改写由模型完成、可能跑几十秒（超过容器单次调用上限），所以后端排队、这里轮询结果。
export function enhanceSkillPrompt(data: { instruction: string; name?: string }): Promise<{ instruction: string }> {
  return request<{ job_id: string }>('/api/skills/enhance', 'POST', data).then(({ job_id }) => new Promise((resolve, reject) => {
    pollJob(job_id, {
      onDone: (result) => resolve({ instruction: String((result && result.instruction) || '') }),
      onError: reject,
    })
  }))
}
// 制作技能：后端 harness 跑完整一轮 skill-creator（可能几分钟）。
// 任务排队执行，过程阶段与成品都用任务轮询取；返回的函数只停止轮询，不取消服务端任务。
export function buildSkill(payload: { instruction: string; name?: string; summary?: string; icon?: string; developer_wechat?: string }, onStage: (label: string) => void, onSkill: (skill: Skill & { note?: string }) => void, onError: (error: any) => void, onDone: () => void) {
  let cancelPoll: (() => void) | null = null
  let finished = false
  const fail = (error: any) => { if (!finished) { finished = true; onError(error) } }
  ensureAuth()
    .then(() => request<{ job_id: string }>('/api/skills/build', 'POST', payload))
    .then(({ job_id }) => {
      if (finished) return
      cancelPoll = pollJob(job_id, {
        onProgress: onStage,
        onDone: (result) => {
          if (finished) return
          finished = true
          const skill = (result && result.skill) || {}
          if (result && result.note) skill.note = result.note
          onSkill(skill)
          onDone()
        },
        onError: fail,
      }, 2000)
    })
    .catch(fail)
  return () => { finished = true; if (cancelPoll) cancelPoll() }
}
export const logout = () => request<any>('/api/auth/logout', 'POST').catch(() => ({ ok: false }))
export type Folder = { id: string; name: string; document_count: number }
export const getFolders = (knowledgeId: string) => request<Folder[]>(`/api/knowledge/${knowledgeId}/folders`)
// 推荐问题：不传 folderId 时按整个知识库生成，传了则收窄到该文件夹。
// 首次生成由模型完成，后端排队执行，这里等任务结果；命中缓存时后端直接返回。
export function getSuggestions(knowledgeId: string, folderId = ''): Promise<{ questions: string[] }> {
  const path = `/api/knowledge/${knowledgeId}/suggestions${folderId ? `?folder_id=${encodeURIComponent(folderId)}` : ''}`
  return request<{ questions: string[]; pending?: boolean; job_id?: string }>(path).then((result) => {
    if (!result.pending || !result.job_id) return { questions: result.questions || [] }
    return new Promise<{ questions: string[] }>((resolve) => {
      pollJob(result.job_id as string, {
        onDone: (done) => resolve({ questions: (done && done.questions) || [] }),
        // 推荐问题只是锦上添花：生成失败就让页面按「没有推荐」处理，不打扰用户
        onError: () => resolve({ questions: [] }),
      })
    })
  })
}

export const createFolder = (knowledgeId: string, name: string) => request<Folder>(`/api/knowledge/${knowledgeId}/folders`, 'POST', { name })
export const deleteFolder = (folderId: string) => request<any>(`/api/folders/${folderId}`, 'DELETE')
export const moveDocument = (documentId: string, folderId: string) => request<any>(`/api/documents/${documentId}/move`, 'PATCH', { folder_id: folderId })
export const importArticle = (knowledgeId: string, url: string, folderId = '') => request<any>(`/api/knowledge/${knowledgeId}/import-article`, 'POST', { url, folder_id: folderId })
// 进会话页时预热后端运行时（官方 SDK 的冷启动 0.7–3.8s），失败静默：不预热也能正常问答
export const preheatHarness = () => request<any>('/api/harness/preheat', 'POST', {})
export type MembershipPlan = 'plus_monthly' | 'plus_quarterly' | 'plus_yearly' | 'pro_monthly' | 'pro_quarterly' | 'pro_yearly'
export type PayPlanOption = { id: MembershipPlan; amount: number; price: string; days: number; available: boolean }
export type PayTier = { id: 'plus' | 'pro'; label: string; storage_bytes: number; storage_label: string; knowledge_bases: number; monthly_credits: number; max_file_label: string; plans: PayPlanOption[] }
export type PayCatalog = { trial_days: number; free: { label: string; storage_label: string; knowledge_bases: number; monthly_credits: number; monthly_credits_after_trial: number }; tiers: PayTier[] }
export const getPayPlans = () => rawRequest<PayCatalog>('/api/pay/plans')
export type LegacyMembershipPlan = MembershipPlan
export const createPayOrder = (plan: MembershipPlan) => request<any>('/api/pay/orders', 'POST', { plan })
// 微信虚拟支付在 iOS 端要求微信客户端 >= 8.0.68，版本不足时直接拦截并提示升级
function ensureVirtualPaymentSupported(): void {
  const api = wx as any
  // 平台已拆分 getSystemInfoSync：设备字段看 device，微信客户端版本看 appBase。
  const device = typeof api.getDeviceInfo === 'function' ? api.getDeviceInfo() : wx.getSystemInfoSync()
  const appBase = typeof api.getAppBaseInfo === 'function' ? api.getAppBaseInfo() : device
  if (device.platform !== 'ios') return
  const current = String(appBase.version || device.version || '').split('.').map(Number)
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
  // 相册/相机的隐私项没在后台申报时，微信不会弹授权弹窗：直接给出能走通的替代入口
  if (isPrivacyScopeError(error)) return new Error('微信没有返回相册/相机权限：可在右上角「···」→「设置」里检查，或改用「从微信文件选择」')
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

// 相册 / 拍照 / 微信文件都属于微信隐私接口：调用前必须已取得用户对
// 《小程序用户隐私保护指引》的同意，否则微信会直接拒绝调用。
// 这里先兜底校验一次，没同意就直接抛错，由调用方给出可读提示。
function chooseLocalUpload(source: UploadSource): Promise<LocalUpload> {
  return ensurePrivacyAuthorized().then((granted) => {
    // 打上 privacy 标记，调用方可以弹「去同意」而不是干巴巴的 toast
    if (!granted) throw Object.assign(new Error('需要先同意《小程序用户隐私保护指引》才能选择资料'), { privacy: true })
    return pickLocalUpload(source)
  })
}

function pickLocalUpload(source: UploadSource): Promise<LocalUpload> {
  if (source === 'file') return new Promise((resolve, reject) => wx.chooseMessageFile({
    count: 1, type: 'file', extension: ['pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'txt', 'md', 'markdown', 'csv', 'jpg', 'jpeg', 'png', 'webp'],
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
  return ensureAuth().then(async () => {
    const slot = await acquireUploadSlot('document', file.filename, { knowledge_id: knowledgeId, folder_id: folderId })
    wx.showLoading({ title: '正在上传', mask: true })
    try {
      await uploadToCos(file.path, slot)
      return await request<any>(`/api/knowledge/${knowledgeId}/documents/${slot.document_id}/complete`, 'POST', { key: slot.key })
    } finally {
      wx.hideLoading()
    }
  })
}

export function uploadLocalFile(knowledgeId: string, path: string, filename: string, folderId = ''): Promise<any> {
  return uploadPickedFile(knowledgeId, { path, filename }, folderId)
}

export async function uploadDocument(knowledgeId: string, source?: UploadSource, folderId = ''): Promise<any> {
  const selected = source || await selectUploadSource()
  const file = await chooseLocalUpload(selected)
  return uploadPickedFile(knowledgeId, file, folderId)
}
export type ChatRunSnapshot = {
  id: string
  conversation_id: string
  user_message_id: string
  knowledge_id: string
  folder_id: string
  status: 'running' | 'completed' | 'error' | 'cancelled' | 'interrupted'
  model: string
  mode: string
  thinking: string
  answer: string
  sources: Source[]
  trace: TraceItem[]
  reason: string
  artifacts: ArtifactView[]
  error: string
  revision: number
  duration_ms: number
  created_at: string
  updated_at: string
  finished_at: string
}

export const startChatRun = (payload: { knowledge_id?: string; conversation_id?: string; folder_id?: string; content: string; model?: string; mode?: 'knowledge' | 'web'; thinking?: 'quick' | 'deep'; skill?: string; skills?: string[] }) =>
  request<ChatRunSnapshot>('/api/chat/runs', 'POST', payload)
export const getChatRun = (runId: string) => request<ChatRunSnapshot>(`/api/chat/runs/${runId}`)
export const getActiveChatRun = (conversationId: string) => request<ChatRunSnapshot | null>(`/api/conversations/${conversationId}/active-run`)
export const stopChatRun = (runId: string) => request<ChatRunSnapshot>(`/api/chat/runs/${runId}/stop`, 'POST', {})

type ChatRunHandlers = {
  onSnapshot: (run: ChatRunSnapshot) => void
  onDone: () => void
  onError: (error: any) => void
}

// 对话过程的轮询间隔：服务端每 ~0.2s 落一次快照。取 600ms 是「跟手」与「请求量」的折中
// ——1s 会在长回答下看出明显的成段跳字，再快就只是白刷接口。
const CHAT_POLL_INTERVAL = 600
const CHAT_POLL_MAX_FAILURES = 8

/**
 * 订阅一个已经启动的服务端聊天任务：轮询 run 快照，revision 变化就回调。
 *
 * 用轮询而不是 SSE：云托管私有协议单次调用上限 15s，而一轮回答动辄几分钟，
 * 长连接拿不到。返回的函数只停止轮询，不停止服务端任务。
 */
export function followChatRun(runId: string, handlers: ChatRunHandlers): () => void {
  let finished = false
  let timer: any = null
  let revision = -1
  let failures = 0

  const stop = () => {
    finished = true
    if (timer) { clearTimeout(timer); timer = null }
  }

  const fail = (error: any) => {
    if (finished) return
    stop()
    handlers.onError(error)
  }

  const tick = () => {
    if (finished) return
    getChatRun(runId).then((run) => {
      if (finished) return
      failures = 0
      const next = Number(run.revision || 0)
      if (next !== revision) {
        revision = next
        handlers.onSnapshot(run)
      }
      if (run.status === 'running') {
        timer = setTimeout(tick, CHAT_POLL_INTERVAL)
        return
      }
      stop()
      handlers.onDone()
    }).catch((error) => {
      if (finished) return
      // 弱网下轮询失败不该把整轮回答判死：退避重试，连续失败才报错
      failures += 1
      if (failures >= CHAT_POLL_MAX_FAILURES) { fail(error); return }
      timer = setTimeout(tick, CHAT_POLL_INTERVAL * failures)
    })
  }

  tick()
  return stop
}

export function streamChat(payload: { knowledge_id?: string; conversation_id?: string; folder_id?: string; content: string; model?: string; mode?: 'knowledge' | 'web'; thinking?: 'quick' | 'deep'; skill?: string; skills?: string[] }, onMeta: (value: any) => void, onDelta: (value: string) => void, onDone: () => void, onError: (error: any) => void, _onProgress?: (label: string) => void, onTrace?: (item: TraceItem) => void, onArtifact?: (artifact: ArtifactView) => void, onSnapshot?: (run: ChatRunSnapshot) => void) {
  let follow: (() => void) | null = null
  let finished = false
  let previousAnswer = ''
  let previousTrace: TraceItem[] = []
  let previousArtifacts: ArtifactView[] = []
  const fail = (error: any) => { if (!finished) { finished = true; onError(error) } }

  ensureAuth()
    .then(() => startChatRun(payload))
    .then((run) => {
      if (finished) return
      onMeta({ conversation_id: run.conversation_id, sources: run.sources, run })
      previousAnswer = run.answer || ''
      previousTrace = run.trace || []
      previousArtifacts = run.artifacts || []
      follow = followChatRun(run.id, {
        onSnapshot: (snapshot) => {
          if (finished) return
          if (onSnapshot) {
            onSnapshot(snapshot)
          } else {
            const next = snapshot.answer || ''
            if (next.startsWith(previousAnswer)) onDelta(next.slice(previousAnswer.length))
            previousAnswer = next
            if (onTrace) {
              const count = previousTrace.length
              ;(snapshot.trace || []).slice(count).forEach((item) => onTrace(item))
            }
            previousTrace = snapshot.trace || []
            if (onArtifact) {
              const seen = new Set(previousArtifacts.map((item) => item.id))
              ;(snapshot.artifacts || []).forEach((item) => { if (!seen.has(item.id)) onArtifact(item) })
            }
            previousArtifacts = snapshot.artifacts || []
          }
        },
        onDone: () => { if (!finished) { finished = true; onDone() } },
        onError: (error) => fail(error),
      })
    })
    .catch(fail)

  return () => { finished = true; if (follow) follow() }
}

// 计划评审：把用户在计划卡片上的结论回写给后端（后端转给运行时，
// 由 @cola/dsh-plan-bridge 交回被阻塞的 exit_plan_mode）。
export const reviewPlan = (reviewId: string, approved: boolean, feedback = '') =>
  request<{ accepted: boolean; reason: string }>('/api/chat/plan-review', 'POST', { review_id: reviewId, approved, feedback })
