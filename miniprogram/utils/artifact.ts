/**
 * 对话产物（agent 生成的文件）在小程序里的打开方式。
 *
 * 后端把「本轮工作区里新生成的文件」收成产物后随消息下发，前端只做两件事：
 * 展示成文件卡，以及按类型用最合适的方式打开。不同格式的打开路径并不一样，
 * 这里统一收口，页面只调 openArtifact / saveArtifact：
 *
 *  · 图片        → 下到本地后走 wx.previewImage（可双指缩放、长按保存）
 *  · PDF/Office → 下到本地后交给微信内置文档渲染器（1:1 版式，右上角菜单可保存/转发）
 *  · 文本类      → 站内预览页（Markdown 正文渲染，代码等宽显示）
 *  · 其它        → 直接转发到微信聊天（保存到手机 / 用其它应用打开）
 *
 * 下载一律走带 Authorization 的 wx.downloadFile：产物按用户收窄，别的账号拿不到文件。
 */
import { ArtifactView, downloadArtifact } from '../services/api'
import { fileIconName, fileTypeLabel } from './file-type'

const IMAGE_TYPES = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']
const OFFICE_TYPES = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx']

/** 卡片左侧的类型图标：与知识库列表、最近页同一套 */
export function artifactIcon(item: ArtifactView | any): string {
  const suffix = String((item && (item.suffix || item.type)) || '')
  return fileIconName(suffix)
}

/** 卡片副标题：类型 · 体积 · 是否已进知识库 */
export function artifactMeta(item: ArtifactView | any): string {
  const suffix = String((item && (item.suffix || item.type)) || '')
  const parts = [fileTypeLabel(suffix), String((item && item.size_label) || '').trim()]
  const note = String((item && item.note) || '').trim()
  if (note) parts.push(note)
  return parts.filter(Boolean).join(' · ')
}

function toast(title: string) {
  wx.showToast({ title, icon: 'none' })
}

function busy<T>(title: string, task: Promise<T>): Promise<T> {
  wx.showLoading({ title, mask: true })
  return task.then(
    (value) => { wx.hideLoading(); return value },
    (error) => { wx.hideLoading(); throw error },
  )
}

/** 转发到微信聊天：用户可以在聊天里继续「保存到手机」或用其它应用打开 */
export function shareArtifactFile(item: ArtifactView, filePath: string): Promise<void> {
  const api = wx as any
  return new Promise<void>((resolve, reject) => {
    if (typeof api.shareFileMessage !== 'function') {
      reject(new Error('当前微信版本不支持转发文件，请在预览页用右上角菜单保存'))
      return
    }
    api.shareFileMessage({
      filePath,
      fileName: item.name,
      success: () => resolve(),
      // 用户取消转发不算失败，不当成错误提示
      fail: (error: any) => {
        const message = String((error && error.errMsg) || '')
        if (message.indexOf('cancel') >= 0) { resolve(); return }
        reject(new Error('转发已取消或不可用'))
      },
    })
  })
}

/** 保存到本地：能走原生菜单的走原生菜单，文本类走转发文件 */
export function saveArtifact(item: ArtifactView) {
  const suffix = String(item.suffix || item.type || '').replace(/^\./, '').toLowerCase()
  busy('正在准备文件', downloadArtifact(item.id)).then(async (filePath) => {
    if (OFFICE_TYPES.indexOf(suffix) >= 0) {
      // 文档类交给微信渲染器打开，界面右上角自带「保存 / 转发」菜单
      wx.openDocument({ filePath, fileType: suffix as any, showMenu: true, fail: () => toast('暂时无法打开该文件') })
      return
    }
    try {
      await shareArtifactFile(item, filePath)
    } catch (error: any) {
      toast((error && error.message) || '保存失败，请稍后重试')
    }
  }).catch((error: any) => toast((error && error.message) || '文件下载失败'))
}

/** 打开产物：能原生预览的就原生预览，其余走站内预览页 */
export function openArtifact(item: ArtifactView) {
  const suffix = String(item.suffix || item.type || '').replace(/^\./, '').toLowerCase()
  if (item.kind === 'text') {
    wx.navigateTo({ url: `/package-features/pages/artifact/index?id=${encodeURIComponent(item.id)}&name=${encodeURIComponent(item.name)}` })
    return
  }
  busy('正在打开文件', downloadArtifact(item.id)).then(async (filePath) => {
    if (IMAGE_TYPES.indexOf(suffix) >= 0) {
      wx.previewImage({ urls: [filePath], current: filePath, fail: () => toast('图片预览失败') })
      return
    }
    if (OFFICE_TYPES.indexOf(suffix) >= 0) {
      wx.openDocument({ filePath, fileType: suffix as any, showMenu: true, fail: () => toast('暂时无法预览该文件，可点右侧保存后查看') })
      return
    }
    try {
      await shareArtifactFile(item, filePath)
    } catch (error: any) {
      toast((error && error.message) || '该格式暂不支持预览，请保存后用其它应用打开')
    }
  }).catch((error: any) => toast((error && error.message) || '文件下载失败'))
}
