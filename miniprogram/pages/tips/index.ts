// 使用技巧详情页：正文与配图全部来自后端 /api/content/tips，
// 改后端 content/ 目录里的 Markdown 即可更新，不需要重新提交小程序。
import { contentAssetUrl, getTipDetail, getTipsIndex } from '../../services/api'
import { applyLocalized, localizeImages } from '../../services/media'

const withKeys = (list: any[]) => (list || []).map((item: any, index: number) => ({
  ...item,
  key: `${item.type}-${index}`,
  // 正文配图同样由后端下发相对路径，这里必须拼成完整地址，否则小程序会当作包内本地图片找不到
  src: item.src ? contentAssetUrl(item.src) : item.src,
  runs: (item.runs || []).map((run: any, runIndex: number) => ({ ...run, key: `${index}-${runIndex}` })),
}))

Page({
  data: {
    loadState: 'loading',
    entry: {} as any,
    blocks: [] as any[],
    related: [] as any[],
  },
  onLoad(query: any) {
    const id = query.id || ''
    this.load(id)
  },
  load(id: string) {
    if (!id) {
      this.setData({ loadState: 'error' })
      return
    }
    this.setData({ loadState: 'loading' })
    Promise.all([getTipDetail(id), getTipsIndex().catch(() => ({ groups: [] } as any))])
      .then((pair) => {
        // 不用数组解构：开发者工具 babel 会为解构注入 slicedToArray helper，注入失败会整页白屏
        const detail = pair[0]
        const index = pair[1]
        const flat = ((index as any).groups || []).reduce((all: any[], group: any) => all.concat(
          (group.entries || []).map((entry: any) => ({ ...entry, cover: contentAssetUrl(entry.cover) })),
        ), [])
        wx.setNavigationBarTitle({ title: (detail as any).title || '使用技巧' })
        const entry = { ...detail, cover: contentAssetUrl((detail as any).cover) }
        const blocks = withKeys((detail as any).blocks || [])
        const related = flat.filter((item: any) => item.id !== id).slice(0, 3)
        this.setData({ loadState: 'ready', entry, blocks, related })
        // 配图统一换成可直接渲染的本地地址：取不到的写成空串，页面直接隐藏，不留灰框
        localizeImages([
          entry.cover,
          ...related.map((item: any) => item.cover),
          ...blocks.map((block: any) => block.src),
        ]).then((map) => this.setData({
          'entry.cover': applyLocalized(entry.cover, map),
          blocks: blocks.map((block: any) => (block.src ? { ...block, src: applyLocalized(block.src, map) } : block)),
          related: related.map((item: any) => (item.cover ? { ...item, cover: applyLocalized(item.cover, map) } : item)),
        })).catch(() => {})
      })
      .catch(() => this.setData({ loadState: 'error' }))
  },
  retry() { this.load(this.data.entry.id) },
  openTip(e: any) {
    const id = e.currentTarget.dataset.id
    if (!id) return
    wx.redirectTo({ url: `/pages/tips/index?id=${id}` })
  },
  askCola() { wx.switchTab({ url: '/pages/ask/index' }) },
})
