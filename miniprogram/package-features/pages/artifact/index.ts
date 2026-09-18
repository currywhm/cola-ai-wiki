/**
 * 生成文件的站内预览页（工具产物里的文本类）。
 *
 * 图片与 PDF/Office 交给微信原生预览，不走这里；Markdown、纯文本、代码、JSON 这些
 * 微信渲染器打不开的类型，在这里直接看正文——不下载、不装应用也能读。
 * 正文由后端 /api/artifacts/{id}/preview 提供，同样按用户收窄。
 */
import { getArtifactPreview } from '../../../services/api'
import { renderMarkdown } from '../../../utils/markdown'
import { artifactIcon, artifactMeta, saveArtifact } from '../../../utils/artifact'
import { fileKind } from '../../../utils/file-type'

const MARKDOWN_KINDS = ['md', 'markdown']

function decodeName(value: string): string {
  const raw = String(value || '')
  if (raw.indexOf('%') < 0) return raw
  try { return decodeURIComponent(raw) } catch (e) { return raw }
}

Page({
  data: {
    id: '',
    name: '',
    icon: 'recent-text',
    meta: '',
    state: 'loading' as 'loading' | 'ready' | 'error',
    error: '',
    text: '',
    html: '',
    truncated: false,
    artifact: null as any,
  },
  onLoad(query: any) {
    this.setData({ id: String((query && query.id) || ''), name: decodeName((query && query.name) || '') || '文件预览' })
    this.load()
  },
  load() {
    if (!this.data.id) { this.setData({ state: 'error', error: '缺少文件信息' }); return }
    this.setData({ state: 'loading' })
    getArtifactPreview(this.data.id).then((artifact: any) => {
      const text = String(artifact.text || '')
      this.setData({
        state: 'ready',
        artifact,
        name: artifact.name || this.data.name,
        icon: artifactIcon(artifact),
        meta: artifactMeta(artifact),
        text,
        // Markdown 用会话页同一套排版渲染，其余文本按等宽正文展示
        html: MARKDOWN_KINDS.indexOf(fileKind(artifact.suffix)) >= 0 ? renderMarkdown(text) : '',
        truncated: !!artifact.truncated,
      })
    }).catch((error: any) => {
      this.setData({ state: 'error', error: (error && error.message) || '文件读取失败，请稍后重试' })
    })
  },
  save() {
    const artifact = this.data.artifact
    if (artifact) saveArtifact(artifact)
  },
})
