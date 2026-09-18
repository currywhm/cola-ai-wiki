// 使用技巧详情页：正文与配图全部来自后端 /api/content/tips，
// 改后端 content/ 目录里的 Markdown 即可更新，不需要重新提交小程序。
import { contentAssetUrl, getTipDetail, getTipsIndex } from '../../../services/api'
import { applyLocalized, localizeImages } from '../../../services/media'

const withKeys = (list: any[]) => (list || []).map((item: any, index: number) => ({
  ...item,
  key: `${item.type}-${index}`,
  // 正文配图同样由后端下发相对路径，这里必须拼成完整地址，否则小程序会当作包内本地图片找不到。
  // 但远程地址不能直接交给 <image>：基础库 3.17 起渲染层拒绝 http，会刷警告并留空框。
  // 因此这里只存 srcSource，src 等图片本地化拿到本地文件路径后回填。
  src: '',
  srcSource: item.src ? contentAssetUrl(item.src) : '',
  runs: (item.runs || []).map((run: any, runIndex: number) => ({ ...run, key: `${index}-${runIndex}` })),
}))

Page({
  data: {
    mode: 'index' as 'index' | 'detail',
    loadState: 'loading',
    index: {} as any,
    groups: [] as any[],
    entry: {} as any,
    blocks: [] as any[],
    related: [] as any[],
  },
  onLoad(query: any) {
    const id = String(query?.id || '')
    if (id) {
      this.setData({ mode: 'detail' })
      this.load(id)
      return
    }
    this.setData({ mode: 'index' })
    this.loadIndex()
  },
  loadIndex() {
    this.setData({ loadState: 'loading' })
    getTipsIndex().then((index) => {
      const groups = (index.groups || []).map((group: any) => ({
        ...group,
        entries: (group.entries || []).map((entry: any) => ({ ...entry, coverSource: contentAssetUrl(entry.cover), cover: '' })),
      }))
      this.setData({ loadState: 'ready', index, groups })
      const covers = groups.reduce((all: string[], group: any) => all.concat(
        (group.entries || []).map((entry: any) => entry.coverSource).filter(Boolean),
      ), [])
      if (!covers.length) return
      localizeImages(covers).then((map) => this.setData({
        groups: groups.map((group: any) => ({
          ...group,
          entries: (group.entries || []).map((entry: any) => (entry.coverSource ? { ...entry, cover: applyLocalized(entry.coverSource, map) } : entry)),
        })),
      })).catch(() => {})
    }).catch(() => this.setData({ loadState: 'error' }))
  },
  load(id: string) {
    this.setData({ loadState: 'loading' })
    Promise.all([getTipDetail(id), getTipsIndex().catch(() => ({ groups: [] } as any))])
      .then((pair) => {
        // 不用数组解构：开发者工具 babel 会为解构注入 slicedToArray helper，注入失败会整页白屏
        const detail = pair[0]
        const index = pair[1]
        const flat = ((index as any).groups || []).reduce((all: any[], group: any) => all.concat(
          (group.entries || []).map((entry: any) => ({ ...entry, coverSource: contentAssetUrl(entry.cover), cover: '' })),
        ), [])
        wx.setNavigationBarTitle({ title: (detail as any).title || '使用技巧' })
        const entry = { ...detail, coverSource: contentAssetUrl((detail as any).cover), cover: '' }
        const blocks = withKeys((detail as any).blocks || [])
        const related = flat.filter((item: any) => item.id !== id).slice(0, 3)
        this.setData({ loadState: 'ready', entry, blocks, related })
        // 配图统一换成可直接渲染的本地地址：取不到的写成空串，页面直接隐藏，不留灰框
        localizeImages([
          entry.coverSource,
          ...related.map((item: any) => item.coverSource),
          ...blocks.map((block: any) => block.srcSource),
        ]).then((map) => this.setData({
          'entry.cover': applyLocalized(entry.coverSource, map),
          blocks: blocks.map((block: any) => (block.srcSource ? { ...block, src: applyLocalized(block.srcSource, map) } : block)),
          related: related.map((item: any) => (item.coverSource ? { ...item, cover: applyLocalized(item.coverSource, map) } : item)),
        })).catch(() => {})
      })
      .catch(() => this.setData({ loadState: 'error' }))
  },
  retry() {
    if (this.data.mode === 'index') { this.loadIndex(); return }
    if (this.data.entry.id) this.load(this.data.entry.id)
  },
  openTip(e: any) {
    const id = e.currentTarget.dataset.id
    if (!id) return
    const url = `/package-features/pages/tips/index?id=${id}`
    if (this.data.mode === 'index') { wx.navigateTo({ url }); return }
    wx.redirectTo({ url })
  },
  askCola() { wx.switchTab({ url: '/pages/ask/index' }) },
})
