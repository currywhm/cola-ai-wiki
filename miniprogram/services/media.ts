// 后端下发的配图是相对路径（/api/content/assets/...）或对象存储直链。
// 渲染层不能带登录态、也没法直连后端域名，所以统一先把字节取回来落到本地再渲染：
//   · 库里的小图（使用技巧封面 / 插图）→ 走容器通道拿 base64，几十 KB 级，一次来回搞定；
//   · 对象存储里的图（头像 / 文章配图）→ 换成短时效直链后 wx.downloadFile。
// 取不到时返回空串，页面据此隐藏图片，而不是留一个灰框。
import { resolveAssets } from './api'

const cache: Record<string, string> = {}

// 文件名用地址的哈希：同一张图多次出现只落一次盘，也不会因为参数不同而互相覆盖
function keyOf(src: string): string {
  let hash = 5381
  for (let i = 0; i < src.length; i++) hash = ((hash << 5) + hash + src.charCodeAt(i)) >>> 0
  return hash.toString(16)
}

const MIME_EXTENSION: Record<string, string> = {
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/gif': '.gif',
  'image/webp': '.webp',
  'image/bmp': '.bmp',
}

function extensionOf(src: string, mime?: string): string {
  if (mime && MIME_EXTENSION[mime]) return MIME_EXTENSION[mime]
  const hit = /\.(png|jpe?g|gif|webp|bmp)(?:[?#]|$)/i.exec(src)
  const ext = hit ? hit[1].toLowerCase() : 'png'
  return `.${ext === 'jpeg' ? 'jpg' : ext}`
}

function writeLocal(src: string, data: string, mime?: string): Promise<string> {
  return new Promise((resolve) => {
    try {
      const path = `${wx.env.USER_DATA_PATH}/cola-asset-${keyOf(src)}${extensionOf(src, mime)}`
      wx.getFileSystemManager().writeFile({
        filePath: path,
        data,
        encoding: 'base64' as any,
        success: () => resolve(path),
        fail: () => resolve(''),
      })
    } catch (error) {
      resolve('')
    }
  })
}

function downloadUrl(url: string, timeout = 30000): Promise<string> {
  return new Promise((resolve) => {
    wx.downloadFile({
      url,
      timeout,
      success: (res: any) => resolve(res.statusCode === 200 ? res.tempFilePath || '' : ''),
      fail: () => resolve(''),
    })
  })
}

function fetchAsset(src: string): Promise<string> {
  // 外部图片（模型回答里可能出现）保持原样直连
  if (/^https?:/i.test(src)) return downloadUrl(src)
  return resolveAssets([src]).then((items) => {
    const item = items[src]
    if (!item || item.error) return ''
    if (item.base64) return writeLocal(src, item.base64, item.mime)
    if (item.url) return downloadUrl(item.url)
    return ''
  })
}

/** 把一张后端配图变成可渲染的本地地址；拿不到返回空串。 */
export function localizeImage(src?: string): Promise<string> {
  if (!src) return Promise.resolve('')
  // 包内相对路径本来就能渲染，不用动
  if (!/^https?:/i.test(src) && src.charAt(0) !== '/') return Promise.resolve(src)
  const hit = cache[src]
  if (hit) return Promise.resolve(hit)
  return fetchAsset(src).then((path) => {
    if (path) cache[src] = path
    return path
  })
}

/** 批量本地化：返回 原地址 -> 可渲染地址 的映射（失败的映射为 ''）。 */
export function localizeImages(list: (string | undefined)[]): Promise<Record<string, string>> {
  const unique = Array.from(new Set((list || []).filter(Boolean) as string[]))
  return Promise.all(unique.map((src) => localizeImage(src).then((path) => [src, path] as [string, string])))
    .then((pairs) => pairs.reduce((map, pair) => { map[pair[0]] = pair[1]; return map }, {} as Record<string, string>))
}

/** 按映射替换地址：映射里明确为空串的，说明这张图拿不到，交回调用方隐藏。 */
export function applyLocalized(src: string, map: Record<string, string>): string {
  return Object.prototype.hasOwnProperty.call(map, src) ? map[src] : src
}

/** 富文本里的图片同样要换成可渲染的本地地址，否则正文配图在 rich-text 里是空框。 */
export function localizeHtmlImages(html: string): Promise<string> {
  const source = String(html || '')
  const srcs = (source.match(/src="([^"]+)"/g) || []).map((piece) => piece.slice(5, -1))
  if (!srcs.length) return Promise.resolve(source)
  return localizeImages(srcs).then((map) => source.replace(/src="([^"]+)"/g, (whole, src) => {
    const hit = applyLocalized(src, map)
    return hit ? `src="${hit}"` : whole
  }))
}
