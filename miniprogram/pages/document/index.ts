import { contentAssetBase, getDocument, previewDocument } from '../../services/api'
import { localizeHtmlImages } from '../../services/media'
import { canPreview, previewIconName } from '../../utils/file-type'

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
    pageIndex: 0,
    pageTotal: 1,
    pageContent: '' as string,
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
        const base = contentAssetBase()
        const html = String(document.content_html).replace(/src="\//g, `src="${base}/`)
        this.setData({ document, canPreview: previewable, previewIcon, isHtml: true, textUnavailable: false, htmlContent: html, pageTotal: 1 })
        // 正文里的配图同样要换成可渲染的本地地址，否则 rich-text 里只剩空框
        localizeHtmlImages(html).then((localized) => this.setData({ htmlContent: localized })).catch(() => {})
        return
      }
      const total = Math.max(1, Number(document.page_count) || 1)
      const text = String(document.extracted_text || '')
      // 按页均分只为给一个粗略的定位感；要看真实版式走「原文预览」
      const parts = total > 1 ? Array.from({ length: total }, (_, index) => text.slice(Math.floor(text.length * index / total), Math.floor(text.length * (index + 1) / total))) : [text]
      this.setData({ document, canPreview: previewable, previewIcon, isHtml: false, textUnavailable: !text.trim(), pageTotal: parts.length, pageIndex: 0, pageContent: parts[0] })
      this.parts = parts
    } catch (error: any) {
      wx.showToast({ title: error.message || '文档加载失败', icon: 'none' })
    }
  },
  parts: [''] as string[],
  previous() { const pageIndex = Math.max(0, this.data.pageIndex - 1); this.setData({ pageIndex, pageContent: this.parts[pageIndex] || '' }) },
  next() { const pageIndex = Math.min(this.data.pageTotal - 1, this.data.pageIndex + 1); this.setData({ pageIndex, pageContent: this.parts[pageIndex] || '' }) },
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
