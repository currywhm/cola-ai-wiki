import { localizeHtmlImages, localizeImage } from '../../services/media'
import { extractMarkdownLinks, renderMarkdownBlocks, renderStreamingText, streamingMarkdownBoundary } from '../../utils/markdown'

Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    source: { type: String, value: '' },
    streaming: { type: Boolean, value: false },
  },
  data: {
    blocks: [] as any[],
    links: [] as any[],
    copiedKey: '',
  },
  observers: {
    'source, streaming'() { this.render() },
  },
  lifetimes: {
    attached() { this.render() },
  },
  methods: {
    render() {
      const source = String(this.data.source || '')
      if (!source) {
        ;(this as any).streamBoundary = 0
        ;(this as any).streamBlocks = []
        this.setData({ blocks: [], links: [], copiedKey: '' })
        return
      }
      // 流式期间只看已闭合的空行块：前面的 Markdown 解析一次并缓存，尾部继续轻量显示。
      if (this.data.streaming) {
        const boundary = streamingMarkdownBoundary(source)
        let previous = Number((this as any).streamBoundary || 0)
        let stable = ((this as any).streamBlocks || []) as any[]
        if (boundary < previous) {
          previous = 0
          stable = []
        }
        if (boundary > previous) {
          stable = renderMarkdownBlocks(source.slice(0, boundary))
          ;(this as any).streamBoundary = boundary
          ;(this as any).streamBlocks = stable
        }
        const tail = source.slice(boundary)
        const blocks = stable.slice()
        if (tail) blocks.push({ key: `stream-${boundary}`, type: 'html', html: renderStreamingText(tail) })
        this.setData({ blocks, links: [] })
        return
      }
      ;(this as any).streamBoundary = 0
      ;(this as any).streamBlocks = []
      const token = Date.now()
      ;(this as any).renderToken = token
      const blocks = renderMarkdownBlocks(source)
      const links = extractMarkdownLinks(source)
      this.setData({ blocks, links })
      // 微信基础库对 http 图片有限制；完成态才把图片落成本地临时文件。
      Promise.all(blocks.map((block) => {
        if (block.type === 'image' && block.src) return localizeImage(block.src).then((src) => (src ? { ...block, src } : null))
        if (block.type === 'html' && block.html) return localizeHtmlImages(block.html).then((html) => ({ ...block, html }))
        return Promise.resolve(block)
      })).then((next) => {
        if ((this as any).renderToken !== token) return
        this.setData({ blocks: next.filter(Boolean) })
      }).catch(() => undefined)
    },
    copyCode(e: any) {
      const key = String((e.currentTarget.dataset || {}).key || '')
      const block = (this.data.blocks as any[]).find((item) => item.key === key)
      const code = block && block.code ? String(block.code) : ''
      if (!code) return
      wx.setClipboardData({ data: code, success: () => {
        this.setData({ copiedKey: key })
        setTimeout(() => this.setData({ copiedKey: '' }), 1400)
      } })
    },
    copyLink(e: any) {
      const url = String((e.currentTarget.dataset || {}).url || '')
      if (!url) return
      wx.setClipboardData({ data: url, success: () => wx.showToast({ title: '链接已复制', icon: 'none' }) })
    },
    previewImage(e: any) {
      const src = String((e.currentTarget.dataset || {}).src || '')
      if (!src) return
      wx.previewImage({ current: src, urls: [src] })
    },
  },
})
