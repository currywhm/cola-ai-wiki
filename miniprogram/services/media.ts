// 后端下发的配图是完整 HTTP(S) 地址。微信基础库 3.17 起「渲染层」不再接受 http:// 图片，
// 调试环境里 <image> 直接引用这些地址只会剩一个空框。
// 这里的做法是不把远程地址交给 <image>，而是先把字节取回来落到本地临时文件，再渲染本地路径：
//   · 本地文件在真机与模拟器里都能渲染，和协议无关；
//   · 线上换成正式 HTTPS 域名后同样走这条路，不依赖任何调试用的旁路服务。
// 取不到时返回空串，页面据此隐藏图片，而不是留一个灰框。
const cache: Record<string, string> = {}

// 文件名用地址的哈希：同一张图多次出现只落一次盘，也不会因为参数不同而互相覆盖
function keyOf(src: string): string {
  let hash = 5381
  for (let i = 0; i < src.length; i++) hash = ((hash << 5) + hash + src.charCodeAt(i)) >>> 0
  return hash.toString(16)
}

function extensionOf(src: string): string {
  const hit = /\.(png|jpe?g|gif|webp|bmp)(?:[?#]|$)/i.exec(src)
  const ext = hit ? hit[1].toLowerCase() : 'png'
  return `.${ext === 'jpeg' ? 'jpg' : ext}`
}

function writeLocal(src: string, data: ArrayBuffer): Promise<string> {
  return new Promise((resolve) => {
    try {
      const path = `${wx.env.USER_DATA_PATH}/cola-asset-${keyOf(src)}${extensionOf(src)}`
      wx.getFileSystemManager().writeFile({
        filePath: path,
        data,
        success: () => resolve(path),
        fail: () => resolve(''),
      })
    } catch (e) {
      resolve('')
    }
  })
}

// 走 wx.request 取字节：它按「请求域名」校验，调试环境里 http 后端同样能取到
function download(src: string): Promise<string> {
  return new Promise((resolve) => {
    wx.request({
      url: src,
      method: 'GET',
      responseType: 'arraybuffer',
      timeout: 20000,
      success: (res: any) => {
        const status = Number(res.statusCode || 0)
        if (status < 200 || status >= 300 || !res.data) { resolve(''); return }
        writeLocal(src, res.data).then(resolve)
      },
      fail: () => resolve(''),
    })
  })
}

/** 把一张后端配图变成可渲染的本地地址；拿不到返回空串。 */
export function localizeImage(src?: string): Promise<string> {
  if (!src) return Promise.resolve('')
  // 包内相对路径本来就能渲染，不用动
  if (!/^https?:/i.test(src)) return Promise.resolve(src)
  const hit = cache[src]
  if (hit) return Promise.resolve(hit)
  // http:// 只出现在本地联调：渲染层已经不支持，getImageInfo 也拿不到，直接走字节下载
  if (/^http:/i.test(src)) return download(src).then((path) => { if (path) cache[src] = path; return path })
  // 线上 HTTPS 域名先试官方接口（最快），失败再自己下载
  return new Promise<string>((resolve) => {
    wx.getImageInfo({ src, success: (res: any) => resolve(res.path || ''), fail: () => resolve('') })
  }).then((path) => (path ? path : download(src))).then((path) => {
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
