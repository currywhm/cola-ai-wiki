import { getDocument, previewDocument } from '../../../services/api'
import { localizeHtmlImages } from '../../../services/media'
import { canPreview, previewIconName } from '../../../utils/file-type'

// 站内阅读是连续滚动的，不再用底部翻页条：
//  · 真实分页的文档（PDF 每页之间用换页符分隔）按真实页分节，节首标「第 N 页」，这是真页码；
//  · 拿不到真实分页的（Word / Excel 的页数是换算出来的），只按长度分节，不标页码，不编造分页。
// 分节还带来一件事：长文档可以滑到底再追加，不用一次性把几万字全渲染出来。
const SECTION_CHARS = 1800
const FIRST_SECTIONS = 3
const SECTION_STEP = 3

type Section = { key: number; page: number; text: string }

// 头部那行说明：只有 PDF 的页数是真页数，Word / Excel 后端给的是估算值，不再当成页码写出来。
function metaLabelFor(document: any): string {
  const type = String((document && document.file_type) || '')
  const pages = Number(document && document.page_count) || 0
  if (type === '.pdf' && pages > 1) return `共 ${pages} 页`
  const chars = String((document && document.extracted_text) || '').replace(/\s+/g, '').length
  if (!chars) return ''
  return chars >= 10000 ? `约 ${(chars / 10000).toFixed(1)} 万字` : `约 ${chars} 字`
}

/** 切节：有真实分页就按页切，没有就按长度切。page=0 表示这一节不是真实的一页。 */
function buildSections(text: string): Section[] {
  const raw = String(text || '')
  if (!raw.trim()) return []
  const pages = raw.split('\f')
  const sections: Section[] = []
  if (pages.length > 1) {
    for (let index = 0; index < pages.length; index += 1) {
      const body = pages[index].trim()
      if (!body) continue
      sections.push({ key: sections.length, page: index + 1, text: body })
    }
    if (sections.length) return sections
  }
  for (let start = 0; start < raw.length; start += SECTION_CHARS) {
    sections.push({ key: sections.length, page: 0, text: raw.slice(start, start + SECTION_CHARS) })
  }
  return sections
}

// 文件阅读是从知识库 / 最近钻进来的视图：不保留底部导航（非 tab 页本身没有 tabBar，这里做显式保障）
function hideTabBar(page: any) {
  if (!page || typeof page.getTabBar !== 'function') return
  const bar = page.getTabBar()
  if (bar && typeof bar.setData === 'function') bar.setData({ hidden: true })
}


Page({
  data: {
    id: '' as string,
    document: {} as any,
    metaLabel: '' as string,
    visibleSections: [] as Section[],
    hasMore: false,
    isHtml: false,
    htmlContent: '' as string,
    // 原文预览：PDF / Word / Excel / PPT 交给微信内置渲染器，版式、图表、分页与原文一致
    canPreview: false,
    previewIcon: 'recent-text' as string,
    textUnavailable: false,
    previewing: false,
  },
  onLoad(query: any) { this.setData({ id: query.id }); this.load() },
  onShow() { hideTabBar(this) },
  async load() {
    try {
      const document = await getDocument(this.data.id)
      const previewable = canPreview(document.file_type)
      const previewIcon = previewIconName(document.file_type)
      if (document.file_type === '.html' && document.content_html) {
        const html = String(document.content_html)
        this.setData({ document, canPreview: previewable, previewIcon, isHtml: true, textUnavailable: false, htmlContent: html, metaLabel: metaLabelFor(document) })
        // 正文里的配图是相对路径（/api/article-assets/...）：换成可渲染的本地地址，否则 rich-text 里只剩空框
        localizeHtmlImages(html).then((localized) => this.setData({ htmlContent: localized })).catch(() => {})
        return
      }
      const text = String(document.extracted_text || '')
      const sections = buildSections(text)
      this.sections = sections
      this.setData({
        document, canPreview: previewable, previewIcon, isHtml: false,
        textUnavailable: !text.trim(), metaLabel: metaLabelFor(document),
        visibleSections: sections.slice(0, FIRST_SECTIONS),
        hasMore: sections.length > FIRST_SECTIONS,
      })
      this.scheduleMeasure()
    } catch (error: any) {
      wx.showToast({ title: error.message || '文档加载失败', icon: 'none' })
    }
  },
  sections: [] as Section[],
  // 滑到底再追加：长文档分几次渲染，滚动中途不卡，也不需要用户点「下一页」
  onReachBottom() { this.appendMore() },
  // 兜底：onReachBottom 的触发点由微信掌控，机型/内容差异下有时偏晚。
  // 滚动时用「距离正文底部还有多远」再判一次，两条路都只会调 appendMore，重复调用是幂等的。
  onPageScroll(event: any) {
    if (!this.data.hasMore || !this.reachAt) return
    const top = Number(event && event.scrollTop) || 0
    if (top < this.reachAt) return
    this.appendMore()
  },
  reachAt: 0,
  measureTimer: 0 as any,
  // 渲染后量一次正文底部的位置，换算出「滚到这里就该追加」的阈值；不在滚动事件里反复量，避免抖动
  scheduleMeasure() {
    if (this.measureTimer) clearTimeout(this.measureTimer)
    this.measureTimer = setTimeout(() => { this.measureTimer = 0; this.measure() }, 240)
  },
  measure() {
    if (!this.data.hasMore) { this.reachAt = 0; return }
    let windowHeight = 0
    try {
      const info: any = (wx as any).getWindowInfo ? (wx as any).getWindowInfo() : wx.getSystemInfoSync()
      windowHeight = Number(info && info.windowHeight) || 0
    } catch (error) { windowHeight = 0 }
    wx.createSelectorQuery()
      .select('.document-content').boundingClientRect()
      .selectViewport().scrollOffset()
      .exec((result: any) => {
        const rect = result && result[0]
        const view = result && result[1]
        if (!rect || !view || !rect.height) return
        const bottom = (Number(view.scrollTop) || 0) + Number(rect.bottom || 0)
        this.reachAt = Math.max(0, bottom - windowHeight - 240)
      })
  },
  appendMore() {
    if (!this.data.hasMore || this.appending) return
    this.appending = true
    const next = Math.min(this.sections.length, this.data.visibleSections.length + SECTION_STEP)
    this.setData({ visibleSections: this.sections.slice(0, next), hasMore: next < this.sections.length }, () => {
      this.appending = false
      this.scheduleMeasure()
    })
  },
  appending: false,
  // 原文预览：下原文件后交给微信内置渲染器，这是微信生态里唯一能 1:1 还原 Office / PDF 版式的路径
  previewOriginal() {
    if (this.data.previewing) return
    this.setData({ previewing: true })
    previewDocument(this.data.id, this.data.document.file_type)
      .catch((error: any) => wx.showToast({ title: (error && error.message) || '原文预览失败', icon: 'none' }))
      .then(() => this.setData({ previewing: false }))
  },
  back() { wx.navigateBack() },
})
