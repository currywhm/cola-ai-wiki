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

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function renderInline(text: string): string {
  let out = escapeHtml(text)
  // 行内代码（先于粗斜体，避免内容被二次处理）
  out = out.replace(/`([^`\n]+)`/g, (_m, code) => `<span class="md-code">${code}</span>`)
  // 粗体、斜体、删除线
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  out = out.replace(/__([^_]+)__/g, '<strong>$1</strong>')
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
  out = out.replace(/~~([^~\n]+)~~/g, '<del class="md-del">$1</del>')
  // 链接 [text](url)：小程序内不跳外链，只保留下划线样式
  out = out.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<span class="md-link">$1</span>')
  return out
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
  if (!source) return ''
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const html: string[] = []
  let inCode = false
  let codeLang = ''
  let codeLines: string[] = []
  let listType: 'ul' | 'ol' | '' = ''
  let para: string[] = []

  const flushPara = () => {
    if (para.length) {
      html.push(`<p class="md-p">${para.map(renderInline).join('<br/>')}</p>`)
      para = []
    }
  }
  const closeList = () => {
    if (listType) {
      html.push(`</${listType}>`)
      listType = ''
    }
  }
  const flushCode = () => {
    // div + <br/> 模拟 pre：保留换行与等宽样式，避开 rich-text 不支持的 pre 标签
    const body = codeLines.map(escapeHtml).join('<br/>')
    html.push(`<div class="md-pre${codeLang ? ` lang-${codeLang}` : ''}">${body}</div>`)
    codeLines = []
    codeLang = ''
  }

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]
    const fence = line.match(/^```(\w*)\s*$/)
    if (fence) {
      if (inCode) {
        flushCode()
        inCode = false
      } else {
        flushPara(); closeList()
        codeLang = fence[1] || ''
        inCode = true
      }
      continue
    }
    if (inCode) {
      codeLines.push(line)
      continue
    }

    const trimmed = line.trim()
    if (!trimmed) {
      flushPara(); closeList()
      continue
    }

    // GFM 管道表格：表头行 + 分隔行的组合才认，避免把正文里的 | 当表格
    if (trimmed.includes('|') && i + 1 < lines.length && isDelimiterRow(lines[i + 1])) {
      flushPara(); closeList()
      const head = splitRow(trimmed)
      const rows: string[][] = []
      let cursor = i + 2
      while (cursor < lines.length) {
        const row = lines[cursor].trim()
        if (!row || !row.includes('|')) break
        rows.push(splitRow(row))
        cursor += 1
      }
      const headHtml = head.map((cell) => `<th class="md-th">${renderInline(cell)}</th>`).join('')
      const bodyHtml = rows
        .map((cells) => `<tr>${cells.map((cell) => `<td class="md-td">${renderInline(cell)}</td>`).join('')}</tr>`)
        .join('')
      html.push(`<table class="md-table"><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml}</tbody></table>`)
      i = cursor - 1
      continue
    }

    const heading = trimmed.match(/^(#{1,4})\s+(.*)$/)
    if (heading) {
      flushPara(); closeList()
      const level = heading[1].length
      html.push(`<p class="md-h md-h${level}">${renderInline(heading[2])}</p>`)
      continue
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      flushPara(); closeList()
      html.push('<hr class="md-hr"/>')
      continue
    }

    if (/^>\s?/.test(trimmed)) {
      flushPara(); closeList()
      html.push(`<p class="md-quote">${renderInline(trimmed.replace(/^>\s?/, ''))}</p>`)
      continue
    }

    const ul = trimmed.match(/^[-*•]\s+(.*)$/)
    const ol = trimmed.match(/^\d+[.、]\s+(.*)$/)
    if (ul || ol) {
      flushPara()
      const type = ul ? 'ul' : 'ol'
      if (listType !== type) {
        closeList()
        html.push(`<${type} class="md-list">`)
        listType = type
      }
      html.push(`<li class="md-li">${renderInline((ul || ol)![1])}</li>`)
      continue
    }

    closeList()
    para.push(trimmed)
  }
  flushPara(); closeList()
  if (inCode) flushCode()
  // 无有效块时返回空串，让调用方（item.html || item.content）回退到纯文本
  if (!html.length) return ''
  return `<div class="md">${html.join('')}</div>`
}
