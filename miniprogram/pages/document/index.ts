import { getDocument } from '../../services/api'

Page({
  data: { id: '', document: {} as any, pageIndex: 0, pageTotal: 1, pageContent: '', isHtml: false, htmlContent: '' },
  onLoad(query: any) { this.setData({ id: query.id }); this.load() },
  async load() { try { const document = await getDocument(this.data.id); if (document.file_type === '.html' && document.content_html) { const base = getApp<IAppOption>().globalData.apiBase || ''; this.setData({ document, isHtml: true, htmlContent: String(document.content_html).replace(/src="\//g, `src="${base}/`), pageTotal: 1 }); return } const total = Math.max(1, Number(document.page_count) || 1); const text = String(document.extracted_text || ''); const parts = total > 1 ? Array.from({ length: total }, (_, index) => text.slice(Math.floor(text.length * index / total), Math.floor(text.length * (index + 1) / total))) : [text]; this.setData({ document, pageTotal: parts.length, pageIndex: 0, pageContent: parts[0] || '文档内容暂无可显示文本。' }); this.parts = parts } catch (error: any) { wx.showToast({ title: error.message || '文档加载失败', icon: 'none' }) } },
  parts: [''] as string[],
  previous() { const pageIndex = Math.max(0, this.data.pageIndex - 1); this.setData({ pageIndex, pageContent: this.parts[pageIndex] || '' }) },
  next() { const pageIndex = Math.min(this.data.pageTotal - 1, this.data.pageIndex + 1); this.setData({ pageIndex, pageContent: this.parts[pageIndex] || '' }) },
  back() { wx.navigateBack() },
})
