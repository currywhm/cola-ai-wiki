/**
 * 分享落地页里的文件：好友（未登录）也能直接预览与保存。
 *
 * 和会话里的产物不同，这里走的是公开取件接口 `/api/shares/{id}/files/{index}`：
 * 不需要登录态，也不能越权拿别的文件——后端只认「这张卡片列出的第 index 个文件」，
 * 且必须仍属于卡片作者。所以这里只用 wx.downloadFile 裸下载，不带 Authorization。
 *
 * 打开方式与会话里的产物保持一致：
 *  · 图片        → wx.previewImage（可双指缩放、长按保存）
 *  · PDF/Office → wx.openDocument（微信内置渲染器，版式 1:1，右上角菜单可保存/转发）
 *  · 其它        → wx.shareFileMessage 转发到聊天，或提示先用微信打开
 */
import { shareFileUrl, ShareFileView } from '../services/api'
import { fileIconName, fileTypeLabel, fileKind, PREVIEW_FILE_TYPES } from './file-type'

const IMAGE_KINDS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']

export type ShareFileItem = ShareFileView & { suffix: string; icon: string; meta: string }

/** 体积文案：好友不认识字节数，统一给人能读的量级 */
export function sizeLabel(bytes: number): string {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function suffixOf(item: any): string {
  const explicit = fileKind(String((item && item.suffix) || ''))
  if (explicit) return explicit
  const name = String((item && item.name) || '')
  const at = name.lastIndexOf('.')
  return at >= 0 ? name.slice(at + 1).toLowerCase() : ''
}

/** 把后端返回的文件列表补成界面能直接渲染的样子（图标 + 类型 · 体积） */
export function decorateShareFiles(files: ShareFileView[] | undefined): ShareFileItem[] {
  const list = Array.isArray(files) ? files : []
  const result: ShareFileItem[] = []
  for (let index = 0; index < list.length; index += 1) {
    const item = list[index] || ({} as ShareFileView)
    const suffix = suffixOf(item)
    result.push({
      index: Number(item.index) || index,
      name: String(item.name || '文件'),
      size: Number(item.size) || 0,
      suffix,
      icon: fileIconName(suffix),
      meta: [fileTypeLabel(suffix), sizeLabel(Number(item.size) || 0)].filter(Boolean).join(' · '),
    })
  }
  return result
}

function toast(title: string) {
  wx.showToast({ title, icon: 'none' })
}

/** 公开取件：不带 token，失败时给人能照做的提示 */
export function downloadShareFile(shareId: string, item: ShareFileItem): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    if (!shareId) { reject(new Error('分享已失效')); return }
    wx.downloadFile({
      url: shareFileUrl(shareId, item.index),
      timeout: 120000,
      success: (res: any) => {
        if (res.statusCode === 200 && res.tempFilePath) { resolve(res.tempFilePath); return }
        reject(new Error(res.statusCode === 404 ? '文件已不在服务器上' : `文件下载失败（${res.statusCode}）`))
      },
      fail: () => reject(new Error('文件下载失败，请检查网络后重试')),
    })
  })
}

function forwardFile(name: string, filePath: string): Promise<void> {
  const api = wx as any
  return new Promise<void>((resolve, reject) => {
    if (typeof api.shareFileMessage !== 'function') {
      reject(new Error('当前微信版本不支持转发文件，请在预览页用右上角菜单保存'))
      return
    }
    api.shareFileMessage({
      filePath,
      fileName: name,
      success: () => resolve(),
      fail: (error: any) => {
        // 用户主动取消不算失败，不当成错误弹出来
        if (String((error && error.errMsg) || '').indexOf('cancel') >= 0) { resolve(); return }
        reject(new Error('转发已取消或不可用'))
      },
    })
  })
}

function busy<T>(title: string, task: Promise<T>): Promise<T> {
  wx.showLoading({ title, mask: true })
  return task.then(
    (value) => { wx.hideLoading(); return value },
    (error) => { wx.hideLoading(); throw error },
  )
}

/** 预览：图片走原生预览，Office/PDF 交给微信渲染器，其余转发到聊天 */
export function openShareFile(shareId: string, item: ShareFileItem) {
  busy('正在打开文件', downloadShareFile(shareId, item)).then(async (filePath) => {
    if (IMAGE_KINDS.indexOf(item.suffix) >= 0) {
      wx.previewImage({ urls: [filePath], current: filePath, fail: () => toast('图片预览失败') })
      return
    }
    if (PREVIEW_FILE_TYPES.indexOf(item.suffix) >= 0) {
      wx.openDocument({ filePath, fileType: item.suffix as any, showMenu: true, fail: () => toast('暂时无法预览该文件，可点右侧保存后查看') })
      return
    }
    try {
      await forwardFile(item.name, filePath)
    } catch (error: any) {
      toast((error && error.message) || '该格式暂不支持预览，请保存后用其它应用打开')
    }
  }).catch((error: any) => toast((error && error.message) || '文件下载失败'))
}

/** 保存：能走原生菜单的交给微信菜单，其余转发到聊天里由用户自己存 */
export function saveShareFile(shareId: string, item: ShareFileItem) {
  busy('正在准备文件', downloadShareFile(shareId, item)).then(async (filePath) => {
    if (PREVIEW_FILE_TYPES.indexOf(item.suffix) >= 0) {
      wx.openDocument({ filePath, fileType: item.suffix as any, showMenu: true, fail: () => toast('暂时无法打开该文件') })
      return
    }
    try {
      await forwardFile(item.name, filePath)
    } catch (error: any) {
      toast((error && error.message) || '保存失败，请稍后重试')
    }
  }).catch((error: any) => toast((error && error.message) || '文件下载失败'))
}
