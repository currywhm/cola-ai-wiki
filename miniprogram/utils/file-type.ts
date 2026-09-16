/**
 * 文件类型在界面上的统一叫法。
 *
 * 知识库目录、文件夹列表、文件阅读页共用这一份映射：同一种后缀在三个入口必须
 * 显示同样的角标，新增类型时也只改一处，不再各页各写一套三元表达式。
 */

const IMAGE_KINDS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'heic']

const TYPE_LABELS: Record<string, string> = {
  png: '图片', jpg: '图片', jpeg: '图片', gif: '图片', webp: '图片', bmp: '图片', heic: '图片',
  pdf: 'PDF',
  doc: 'DOC', docx: 'DOC',
  md: 'MD', markdown: 'MD',
  txt: 'TXT',
  ppt: 'PPT', pptx: 'PPT',
  xls: '表格', xlsx: '表格', csv: '表格',
  html: '推文',
}

/** 去掉点号并转小写：file_type 一律是 ".pdf" 这种形式 */
export function fileKind(fileType: string): string {
  return String(fileType || '').replace(/^\./, '').toLowerCase()
}

/** 文件行左侧的图标分组：图片走图片图标，PDF 有专属色，其余靠角标文字区分 */
export function fileTypeKind(fileType: string): 'image' | 'pdf' | 'other' {
  const kind = fileKind(fileType)
  if (IMAGE_KINDS.indexOf(kind) >= 0) return 'image'
  if (kind === 'pdf') return 'pdf'
  return 'other'
}

/** 文件行上的类型角标 */
export function fileTypeLabel(fileType: string): string {
  return TYPE_LABELS[fileKind(fileType)] || '文件'
}

/**
 * 微信内置渲染器能打开的文档类型。
 * 这些类型走「原文预览」：版式、图片、表格、分页都按原文件呈现，
 * 是微信生态里唯一能 1:1 还原 Office / PDF 版式的路径。
 */
export const PREVIEW_FILE_TYPES = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx']

/** 该类型能否原文预览 */
export function canPreview(fileType: string): boolean {
  return PREVIEW_FILE_TYPES.indexOf(fileKind(fileType)) >= 0
}

/** 预览入口上的类型图标，与知识库列表里文件行的图标保持一致 */
const PREVIEW_ICONS: Record<string, string> = {
  pdf: 'recent-pdf',
  doc: 'recent-doc',
  docx: 'recent-doc',
  ppt: 'recent-ppt',
  pptx: 'recent-ppt',
  xls: 'recent-sheet',
  xlsx: 'recent-sheet',
}

// 全站统一的后缀 → 图标映射：知识库列表、最近、参考出处、对话产物用的是同一套。
// 新增类型只改这一处，四个入口不会再各写一份表格。
const TYPE_ICONS: Record<string, string> = {
  pdf: 'recent-pdf',
  doc: 'recent-doc', docx: 'recent-doc', rtf: 'recent-doc',
  ppt: 'recent-ppt', pptx: 'recent-ppt', key: 'recent-ppt',
  xls: 'recent-sheet', xlsx: 'recent-sheet', csv: 'recent-sheet', tsv: 'recent-sheet',
  png: 'recent-image', jpg: 'recent-image', jpeg: 'recent-image', gif: 'recent-image', webp: 'recent-image', bmp: 'recent-image',
  zip: 'recent-folder', folder: 'recent-folder',
}

/** 文件在列表 / 卡片上用的类型图标（叫不出来的统统按文本处理） */
export function fileIconName(fileType: string): string {
  return TYPE_ICONS[fileKind(fileType)] || 'recent-text'
}

export function previewIconName(fileType: string): string {
  return PREVIEW_ICONS[fileKind(fileType)] || 'recent-text'
}
