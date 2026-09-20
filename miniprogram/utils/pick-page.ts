/**
 * 三个会话页共用的「多选 → 分享」控制器。
 *
 * 页面只负责把事件转发进来（长按进多选、点勾选、点全选、点分享面板的去处），
 * 选中态、勾选映射、分享文案、复制到剪贴板、存到知识库都在这里收口，
 * 这样问AI / 知识库 / 文件夹三个入口的行为与文案不会各写一套。
 */
import { getKnowledge, saveSelectionToKnowledge } from '../services/api'
import { copyText } from './clipboard'
import { buildSelectionPayload, SharePayload } from './share'
import { canPickMessage, collectSelection, fileKey, messageKey, pickableKeys, PickSelection, shareHint, PICK_FILE_PREFIX, PICK_MSG_PREFIX } from './pick'

export function initialPickData() {
  return {
    pickMode: false,
    pickedKeys: [] as string[],
    pickedMap: {} as Record<string, boolean>,
    pickCount: 0,
    pickTotal: 0,
    shareSheetVisible: false,
    shareKnowledges: [] as any[],
    shareKnowledgeLoading: false,
    shareHintText: '',
    shareBusy: false,
  }
}

function selectionOf(page: any, options: { plain?: boolean } = {}): PickSelection {
  const data = page.data || {}
  return collectSelection(data.messages || [], data.pickedKeys || [], data.knowledgeName || '', options)
}

/** 长按只对能分享的内容生效：开场白、本地提示、正在生成的空回答都不进多选。 */
function canPick(page: any, key: string): boolean {
  if (key.indexOf(PICK_FILE_PREFIX) === 0) return true
  const id = key.slice(PICK_MSG_PREFIX.length)
  const messages = (page.data && page.data.messages) || []
  for (let index = 0; index < messages.length; index += 1) {
    if (String(messages[index].id) === id) return canPickMessage(messages[index])
  }
  return false
}

export function payloadOf(page: any): SharePayload {
  const data = page.data || {}
  return buildSelectionPayload({
    messages: data.messages || [],
    keys: data.pickedKeys || [],
    knowledgeName: data.knowledgeName || '',
  })
}

/** 微信转发前先把完整选中快照固定下来，避免转发回调阶段页面状态已变化。 */
export function armWechat(page: any): SharePayload {
  const payload = payloadOf(page)
  ;(page as any).pickWechatPayload = payload
  return payload
}

export function takeWechatPayload(page: any): SharePayload | null {
  const payload = (page as any).pickWechatPayload as SharePayload | undefined
  const snapshot = (page as any).pickSelectionSnapshot as { messages: any[]; keys: string[]; knowledgeName?: string } | undefined
  ;(page as any).pickWechatPayload = null
  ;(page as any).pickSelectionSnapshot = null
  if (payload) return payload
  return snapshot ? buildSelectionPayload(snapshot) : null
}

/** 重新算一遍勾选映射与计数：WXML 只能读现成的布尔值，不能自己算 indexOf。 */
export function refresh(page: any): void {
  const data = page.data || {}
  const keys: string[] = data.pickedKeys || []
  const map: Record<string, boolean> = {}
  for (let index = 0; index < keys.length; index += 1) map[keys[index]] = true
  const selection = selectionOf(page)
  page.setData({
    pickedMap: map,
    pickCount: keys.length,
    pickTotal: pickableKeys(data.messages || []).length,
    pickMessageCount: selection.messageCount,
    pickFileCount: selection.fileCount,
    shareHintText: shareHint('wechat', selection),
  })
}


export function enter(page: any, key: string): void {
  if (!key || page.data.sending || !canPick(page, key)) return
  ;(page as any).pickSelectionSnapshot = null
  ;(page as any).pickWechatPayload = null
  page.setData({ pickMode: true, pickedKeys: [key] }, () => refresh(page))
  wx.vibrateShort({ type: 'light' } as any)
}

export function exit(page: any): void {
  ;(page as any).pickSelectionSnapshot = null
  ;(page as any).pickWechatPayload = null
  page.setData({ pickMode: false, pickedKeys: [], pickedMap: {}, pickCount: 0, shareSheetVisible: false })
}

export function toggle(page: any, key: string): void {
  if (!key) return
  const keys: string[] = (page.data.pickedKeys || []).slice()
  const at = keys.indexOf(key)
  if (at >= 0) keys.splice(at, 1)
  else keys.push(key)
  page.setData({ pickedKeys: keys }, () => refresh(page))
}

export function toggleAll(page: any): void {
  const keys: string[] = page.data.pickedKeys || []
  const total = pickableKeys(page.data.messages || [])
  if (keys.length && keys.length === total.length) {
    page.setData({ pickedKeys: [] }, () => refresh(page))
    return
  }
  page.setData({ pickedKeys: total }, () => refresh(page))
}

export function openSheet(page: any): void {
  const selection = selectionOf(page)
  if (!selection.count) {
    wx.showToast({ title: '先勾选要分享的内容', icon: 'none' })
    return
  }
  ;(page as any).pickSelectionSnapshot = {
    messages: ((page.data && page.data.messages) || []).slice(),
    keys: ((page.data && page.data.pickedKeys) || []).slice(),
    knowledgeName: (page.data && page.data.knowledgeName) || '',
  }
  page.setData({ shareSheetVisible: true, shareHintText: shareHint('wechat', selection) })
}

export function closeSheet(page: any): void {
  ;(page as any).pickSelectionSnapshot = null
  page.setData({ shareSheetVisible: false })
}

/** QQ / 钉钉只能复制内容：微信不允许小程序直接跳转别的 App，这一点在提示里说清楚。 */
export function copyFor(page: any, target: string): void {
  // 复制到剪贴板用渲染后的纯文本：与回答下方的「复制」保持一致，粘贴出来不带 Markdown 标记
  const selection = selectionOf(page, { plain: true })
  if (!selection.count) return
  if (!selection.messageCount) {
    wx.showToast({ title: `只有文件，${target === 'qq' ? 'QQ' : '钉钉'}收不到，改用微信或存到知识库`, icon: 'none', duration: 2600 })
    return
  }
  const name = target === 'qq' ? 'QQ' : '钉钉'
  const tail = selection.fileCount ? `，勾选的 ${selection.fileCount} 个文件请用微信或存到知识库` : ''
  copyText(selection.text + '\n\n—— 来自 cola知识库', `已复制，打开${name}粘贴即可${tail}`)
}

/** 面板第二层：读一下用户的知识库，给一个可点的列表。 */
export function loadKnowledges(page: any): void {
  page.setData({ shareKnowledgeLoading: true })
  getKnowledge()
    .then((list) => {
      page.setData({ shareKnowledges: Array.isArray(list) ? list : [], shareKnowledgeLoading: false })
    })
    .catch(() => {
      page.setData({ shareKnowledgeLoading: false })
      wx.showToast({ title: '知识库读取失败，请稍后重试', icon: 'none' })
    })
}

export function saveToKnowledge(page: any, knowledgeId: string, knowledgeName: string): void {
  const selection = selectionOf(page)
  if (!knowledgeId || !selection.count) return
  if (page.data.shareBusy) return
  const content = (selection.text || '').trim() || (selection.fileCount ? `本次分享了 ${selection.fileCount} 个文件。` : '')
  page.setData({ shareBusy: true })
  wx.showLoading({ title: '正在存入', mask: true })
  saveSelectionToKnowledge({
    knowledge_id: knowledgeId,
    title: selection.title,
    content,
    artifact_ids: selection.artifactIds,
  }, (label) => wx.showLoading({ title: String(label || '正在存入').slice(0, 12), mask: true }))
    .then((result) => {
      wx.hideLoading()
      page.setData({ shareBusy: false, shareSheetVisible: false })
      exit(page)
      wx.showToast({ title: (result && result.message) || `已存入「${knowledgeName}」`, icon: 'none', duration: 2200 })
    })
    .catch((error: any) => {
      wx.hideLoading()
      page.setData({ shareBusy: false })
      wx.showToast({ title: (error && error.message) || '存入失败，请稍后重试', icon: 'none', duration: 2400 })
    })
}

export { fileKey, messageKey }
