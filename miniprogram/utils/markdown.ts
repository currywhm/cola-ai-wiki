/**
 * 轻量 Markdown → HTML 转换（供 rich-text 渲染助手回答）。
 * 支持：标题、粗体、斜体、删除线、行内代码、代码块、无序/有序列表、链接、引用、
 * 分隔线、GFM 管道表格。
 * 参考 deepseek-harness 前端的 markdown 输出呈现，保持小程序端够用、零依赖。
 *
 * 注意：rich-text 的 nodes 字符串只支持受信任标签子集（div/p/span/ul/ol/li/
 * strong/em/del/br/code/table/thead/tbody/tr/th/td/hr 等），不支持 view、pre
 * 等小程序组件标签——真机 WebView 遇到不支持的标签会静默渲染为空白，因此这里严格
 * 只用受信任标签，样式一律挂在 class 上。
 */

export interface MarkdownLink {
  label: string
  url: string
  title?: string
}

export interface MarkdownBlock {
  key: string
  type: 'html' | 'code' | 'image'
  html?: string
  code?: string
  lang?: string
  label?: string
  alt?: string
  src?: string
}

interface MarkdownDefinitions {
  links: Record<string, { url: string; title?: string }>
  footnotes: Record<string, string>
}


function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function escapeAttr(text: string): string {
  return escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;')
}

const SAFE_LINK_RE = /^(https?:\/\/|mailto:)/i
const SAFE_IMAGE_RE = /^(https?:\/\/|\/|data:image\/)/i
const FOOTNOTE_LINE_RE = /^\[\^([^\]]+)\]:\s*(.+)$/
const REFERENCE_LINE_RE = /^\[([^\]]+)\]:\s*(\S+)(?:\s+["'(](.+?)["')])?\s*$/

function safeUrl(url: string, image = false): string {
  const value = String(url || '').trim()
  return (image ? SAFE_IMAGE_RE : SAFE_LINK_RE).test(value) ? value : ''
}

function plainLabel(text: string): string {
  return String(text || '').replace(/[*_~`]/g, '').replace(/\s+/g, ' ').trim()
}

interface InlineRenderState {
  refs: MarkdownDefinitions
  protected: string[]
}

function protectHtml(state: InlineRenderState, html: string): string {
  const token = `@@MD-PROTECTED-${state.protected.length}@@`
  state.protected.push(html)
  return token
}

function restoreProtected(html: string, state: InlineRenderState): string {
  let out = html
  state.protected.forEach((piece, index) => { out = out.replace(`@@MD-PROTECTED-${index}@@`, piece) })
  return out
}

function renderInline(text: string, refs: MarkdownDefinitions = { links: {}, footnotes: {} }): string {
  const state: InlineRenderState = { refs, protected: [] }
  let out = escapeHtml(text)
  // 行内代码先抽走，后续粗体/公式规则不会误伤代码里的 * 或 $。
  out = out.replace(/`([^`\n]+)`/g, (_m, code) => protectHtml(state, `<span class="md-code">${code}</span>`))
  out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+["'(](.*?)["')])?\)/g, (match, alt, src, title) => {
    const url = safeUrl(src, true)
    if (!url) return match
    const titleAttr = title ? ` title="${escapeAttr(title)}"` : ''
    return protectHtml(state, `<img class="md-image-inline" src="${escapeAttr(url)}" alt="${escapeAttr(alt || '')}"${titleAttr}/>`)
  })
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|mailto:[^)\s]+)(?:\s+["'(](.*?)["')])?\)/g, (match, label, url, title) => {
    if (!safeUrl(url)) return match
    const titleAttr = title ? ` title="${escapeAttr(title)}"` : ''
    return protectHtml(state, `<span class="md-link"${titleAttr}>${label}</span>`)
  })
  out = out.replace(/\[([^\]]+)\]\[([^\]]+)\]/g, (match, label, id) => {
    if (!refs.links[String(id).toLowerCase()]) return match
    return protectHtml(state, `<span class="md-link">${label}</span>`)
  })
  out = out.replace(/<((?:https?:\/\/|mailto:)[^>\s]+)>/g, (match, url) => {
    if (!safeUrl(url)) return match
    return protectHtml(state, `<span class="md-link">${escapeHtml(String(url).replace(/^mailto:/i, ''))}</span>`)
  })
  // 小程序没有 KaTeX：公式保留 TeX 原文，避免空白。
  out = out.replace(/\$([^$\n]+)\$/g, (_m, formula) => protectHtml(state, `<span class="md-formula">${formula}</span>`))
  out = out.replace(/\[\^([^\]]+)\]/g, (_m, id) => protectHtml(state, `<span class="md-footnote-ref">[${escapeHtml(String(id))}]</span>`))
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  out = out.replace(/__([^_]+)__/g, '<strong>$1</strong>')
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
  out = out.replace(/~~([^~\n]+)~~/g, '<del class="md-del">$1</del>')
  return restoreProtected(out, state)
}

/** 管道表格的单元格切分：去掉首尾竖线后再按 | 切开 */
function splitRow(line: string): string[] {
  let text = line.trim()
  if (text.startsWith('|')) text = text.slice(1)
  if (text.endsWith('|')) text = text.slice(0, -1)
  return text.split('|').map((cell) => cell.trim())
}

/** 分隔行：| --- | :--: | 之类，用来说明上一行是表头 */
function isDelimiterRow(line: string): boolean {
  const text = line.trim()
  if (!text.includes('-')) return false
  return /^\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?$/.test(text)
}

export function renderMarkdown(source: string): string {
  const blocks = renderMarkdownBlocks(source)
  if (!blocks.length) return ''
  return `<div class="md">${blocks.map((block) => {
    if (block.type === 'code') return `<div class="md-pre">${block.html || ''}</div>`
    if (block.type === 'image') return `<p class="md-p"><img class="md-image" src="${escapeAttr(block.src || '')}" alt="${escapeAttr(block.alt || '')}"/></p>`
    return block.html || ''
  }).join('')}</div>`
}

function collectDefinitions(lines: string[]): { lines: string[]; defs: MarkdownDefinitions } {
  const defs: MarkdownDefinitions = { links: {}, footnotes: {} }
  const body: string[] = []
  lines.forEach((line) => {
    const footnote = FOOTNOTE_LINE_RE.exec(line.trim())
    if (footnote) { defs.footnotes[footnote[1]] = footnote[2].trim(); return }
    const ref = REFERENCE_LINE_RE.exec(line.trim())
    if (ref) {
      const url = safeUrl(ref[2])
      if (url) defs.links[ref[1].toLowerCase()] = { url, title: ref[3] }
      return
    }
    body.push(line)
  })
  return { lines: body, defs }
}

function renderFootnotes(footnotes: Record<string, string>): string {
  const ids = Object.keys(footnotes)
  if (!ids.length) return ''
  return `<div class="md-footnotes">${ids.map((id) => `<div class="md-footnote"><span class="md-footnote-id">[${escapeHtml(id)}]</span><span>${renderInline(footnotes[id])}</span></div>`).join('')}</div>`
}

function renderRichLines(lines: string[], defs: MarkdownDefinitions): string {
  const html: string[] = []
  let listType: 'ul' | 'ol' | '' = ''
  let para: string[] = []
  const flushPara = () => { if (para.length) { html.push(`<p class="md-p">${para.map((line) => renderInline(line, defs)).join('<br/>')}</p>`); para = [] } }
  const closeList = () => { if (listType) { html.push(`</${listType}>`); listType = '' } }
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    const trimmed = line.trim()
    if (!trimmed) { flushPara(); closeList(); continue }
    if (trimmed === '$$' || trimmed === '\\[') {
      flushPara(); closeList()
      const closing = trimmed === '$$' ? '$$' : '\\]'
      const formula: string[] = []
      let cursor = index + 1
      while (cursor < lines.length && lines[cursor].trim() !== closing) { formula.push(lines[cursor]); cursor += 1 }
      html.push(`<div class="md-formula-block">${escapeHtml(formula.join('\n')).replace(/\n/g, '<br/>')}</div>`)
      index = cursor
      continue
    }
    const oneLineFormula = /^\$\$(.+)\$\$$/.exec(trimmed) || /^\\\[(.+)\\\]$/.exec(trimmed)
    if (oneLineFormula) { flushPara(); closeList(); html.push(`<div class="md-formula-block">${escapeHtml(oneLineFormula[1])}</div>`); continue }
    if (trimmed.includes('|') && index + 1 < lines.length && isDelimiterRow(lines[index + 1])) {
      flushPara(); closeList()
      const head = splitRow(trimmed)
      const rows: string[][] = []
      let cursor = index + 2
      while (cursor < lines.length) { const row = lines[cursor].trim(); if (!row || !row.includes('|')) break; rows.push(splitRow(row)); cursor += 1 }
      html.push(`<table class="md-table"><thead><tr>${head.map((cell) => `<th class="md-th">${renderInline(cell, defs)}</th>`).join('')}</tr></thead><tbody>${rows.map((cells) => `<tr>${cells.map((cell) => `<td class="md-td">${renderInline(cell, defs)}</td>`).join('')}</tr>`).join('')}</tbody></table>`)
      index = cursor - 1
      continue
    }
    const heading = trimmed.match(/^(#{1,4})\s+(.*)$/)
    if (heading) { flushPara(); closeList(); html.push(`<p class="md-h md-h${heading[1].length}">${renderInline(heading[2], defs)}</p>`); continue }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) { flushPara(); closeList(); html.push('<hr class="md-hr"/>'); continue }
    if (/^>\s?/.test(trimmed)) { flushPara(); closeList(); html.push(`<p class="md-quote">${renderInline(trimmed.replace(/^>\s?/, ''), defs)}</p>`); continue }
    const task = /^[-*•]\s+\[([ xX])\]\s+(.*)$/.exec(trimmed)
    if (task) {
      flushPara(); closeList()
      const checked = task[1].toLowerCase() === 'x'
      html.push(`<div class="md-task${checked ? ' is-checked' : ''}"><span class="md-task-box">${checked ? '✓' : ''}</span><span class="md-task-text">${renderInline(task[2], defs)}</span></div>`)
      continue
    }
    const unordered = trimmed.match(/^[-*•]\s+(.*)$/)
    const ordered = trimmed.match(/^\d+[.、]\s+(.*)$/)
    if (unordered || ordered) {
      flushPara()
      const type = unordered ? 'ul' : 'ol'
      if (listType !== type) { closeList(); html.push(`<${type} class="md-list">`); listType = type }
      html.push(`<li class="md-li">${renderInline((unordered || ordered)![1], defs)}</li>`)
      continue
    }
    closeList()
    para.push(trimmed)
  }
  flushPara(); closeList()
  return `${html.join('')}${renderFootnotes(defs.footnotes)}`
}

export function renderMarkdownBlocks(source: string): MarkdownBlock[] {
  if (!source) return []
  const collected = collectDefinitions(String(source).replace(/\r\n/g, '\n').split('\n'))
  const lines = collected.lines
  const blocks: MarkdownBlock[] = []
  let buffer: string[] = []
  const flush = () => {
    if (!buffer.length) return
    const html = renderRichLines(buffer, collected.defs)
    if (html) blocks.push({ key: `html-${blocks.length}`, type: 'html', html })
    buffer = []
  }
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    const fence = line.match(/^```(\w*)\s*$/)
    if (fence) {
      flush()
      const code: string[] = []
      const lang = fence[1] || ''
      let cursor = index + 1
      while (cursor < lines.length && !/^```\s*$/.test(lines[cursor])) { code.push(lines[cursor]); cursor += 1 }
      const value = code.join('\n')
      blocks.push({ key: `code-${blocks.length}`, type: 'code', html: highlightCode(value, lang), code: value, lang, label: lang ? lang.toUpperCase() : '代码' })
      index = cursor
      continue
    }
    const image = /^!\[([^\]]*)\]\(([^)\s]+)(?:\s+["'(](.*?)["')])?\)\s*$/.exec(line.trim())
    if (image) {
      const src = safeUrl(image[2], true)
      if (src) { flush(); blocks.push({ key: `image-${blocks.length}`, type: 'image', alt: image[1] || '', src }); continue }
    }
    buffer.push(line)
  }
  flush()
  return blocks
}

export function renderStreamingText(source: string): string {
  if (!source) return ''
  return `<div class="md md-stream">${escapeHtml(source).replace(/\n/g, '<br/>')}</div>`
}

/**
 * 流式渲染的安全边界：只承认代码围栏外的空行。
 *
 * 空行前的 Markdown 已经闭合，可以增量解析并缓存；空行后的尾部仍可能
 * 是半截标题、列表或表格，继续按纯文本显示，等下一批增量到齐后再固化。
 */
export function streamingMarkdownBoundary(source: string): number {
  const text = String(source || '')
  let inFence = false
  let boundary = 0
  let lineStart = 0
  while (lineStart < text.length) {
    const newline = text.indexOf('\n', lineStart)
    const lineEnd = newline < 0 ? text.length : newline
    const line = text.slice(lineStart, lineEnd).trim()
    if (/^```/.test(line)) inFence = !inFence
    if (!inFence && line === '' && newline >= 0) boundary = newline + 1
    if (newline < 0) break
    lineStart = newline + 1
  }
  return boundary
}

export function extractMarkdownLinks(source: string): MarkdownLink[] {
  if (!source) return []
  const withoutCode = String(source).replace(/```[\s\S]*?```/g, '')
  const collected = collectDefinitions(withoutCode.replace(/\r\n/g, '\n').split('\n'))
  const links: MarkdownLink[] = []
  const seen: Record<string, boolean> = {}
  const push = (label: string, url: string, title?: string) => {
    const safe = safeUrl(url)
    if (!safe || seen[safe]) return
    seen[safe] = true
    links.push({ label: plainLabel(label) || safe, url: safe, title })
  }
  withoutCode.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|mailto:[^)\s]+)(?:\s+["'(](.*?)["')])?\)/g, (match, label, url, title) => { push(label, url, title); return match })
  withoutCode.replace(/\[([^\]]+)\]\[([^\]]+)\]/g, (match, label, id) => { const ref = collected.defs.links[String(id).toLowerCase()]; if (ref) push(label, ref.url, ref.title); return match })
  Object.keys(collected.defs.footnotes).forEach((id) => { const match = /(https?:\/\/[^\s]+)/.exec(collected.defs.footnotes[id]); if (match) push(`脚注 ${id}`, match[1]) })
  return links
}

const CODE_KEYWORDS: Record<string, string[]> = {
  bash: ['if', 'then', 'else', 'fi', 'for', 'while', 'do', 'done', 'case', 'esac', 'function', 'export', 'local', 'return', 'echo', 'cd', 'set'],
  shell: ['if', 'then', 'else', 'fi', 'for', 'while', 'do', 'done', 'case', 'esac', 'function', 'export', 'local', 'return', 'echo', 'cd', 'set'],
  js: ['const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while', 'class', 'new', 'async', 'await', 'try', 'catch', 'throw', 'import', 'from', 'export', 'default', 'true', 'false', 'null', 'undefined'],
  javascript: ['const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while', 'class', 'new', 'async', 'await', 'try', 'catch', 'throw', 'import', 'from', 'export', 'default', 'true', 'false', 'null', 'undefined'],
  ts: ['const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while', 'class', 'interface', 'type', 'new', 'async', 'await', 'try', 'catch', 'throw', 'import', 'from', 'export', 'default', 'true', 'false', 'null', 'undefined'],
  typescript: ['const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while', 'class', 'interface', 'type', 'new', 'async', 'await', 'try', 'catch', 'throw', 'import', 'from', 'export', 'default', 'true', 'false', 'null', 'undefined'],
  python: ['def', 'class', 'return', 'if', 'elif', 'else', 'for', 'while', 'try', 'except', 'finally', 'with', 'as', 'import', 'from', 'lambda', 'True', 'False', 'None', 'and', 'or', 'not', 'in', 'is', 'async', 'await'],
  py: ['def', 'class', 'return', 'if', 'elif', 'else', 'for', 'while', 'try', 'except', 'finally', 'with', 'as', 'import', 'from', 'lambda', 'True', 'False', 'None', 'and', 'or', 'not', 'in', 'is', 'async', 'await'],
  json: ['true', 'false', 'null'],
  sql: ['select', 'from', 'where', 'join', 'left', 'right', 'inner', 'outer', 'on', 'group', 'by', 'order', 'limit', 'insert', 'into', 'update', 'delete', 'create', 'table', 'and', 'or', 'null', 'as'],
}

function highlightCode(source: string, lang: string): string {
  const keywords = new Set(CODE_KEYWORDS[String(lang || '').toLowerCase()] || [])
  const hashComment = ['bash', 'shell', 'sh', 'zsh', 'python', 'py', 'yaml', 'yml'].indexOf(String(lang || '').toLowerCase()) >= 0
  const chars = String(source || '')
  let out = ''
  let index = 0
  const append = (cls: string, value: string) => {
    const body = escapeHtml(value).replace(/\t/g, '    ').replace(/ /g, '&nbsp;').replace(/\n/g, '<br/>')
    out += cls ? `<span class="${cls}">${body}</span>` : body
  }
  while (index < chars.length) {
    const rest = chars.slice(index)
    if (rest.startsWith('//')) { const end = chars.indexOf('\n', index); const stop = end < 0 ? chars.length : end; append('tok-comment', chars.slice(index, stop)); index = stop; continue }
    if (rest.startsWith('/*')) { const end = chars.indexOf('*/', index + 2); const stop = end < 0 ? chars.length : end + 2; append('tok-comment', chars.slice(index, stop)); index = stop; continue }
    if (hashComment && chars[index] === '#') { const end = chars.indexOf('\n', index); const stop = end < 0 ? chars.length : end; append('tok-comment', chars.slice(index, stop)); index = stop; continue }
    const char = chars[index]
    if (char === '"' || char === '\'' || char === '`') {
      const quote = char
      let stop = index + 1
      while (stop < chars.length) { if (chars[stop] === '\\' && stop + 1 < chars.length) { stop += 2; continue } if (chars[stop] === quote) { stop += 1; break } stop += 1 }
      append('tok-string', chars.slice(index, stop)); index = stop; continue
    }
    if (/[0-9]/.test(char)) { let stop = index + 1; while (stop < chars.length && /[0-9._]/.test(chars[stop])) stop += 1; append('tok-number', chars.slice(index, stop)); index = stop; continue }
    if (/[A-Za-z_$]/.test(char)) {
      let stop = index + 1
      while (stop < chars.length && /[A-Za-z0-9_$-]/.test(chars[stop])) stop += 1
      const word = chars.slice(index, stop)
      append(keywords.has(word.toLowerCase()) ? 'tok-keyword' : '', word); index = stop; continue
    }
    append('', char)
    index += 1
  }
  return out
}
