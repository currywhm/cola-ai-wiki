/**
 * 轻量 Markdown → HTML 转换（供 rich-text 渲染助手回答）。
 * 支持：标题、粗体、斜体、行内代码、代码块、无序/有序列表、链接、引用、段落。
 * 参考 deepseek-harness 前端的 markdown 输出呈现，保持小程序端够用、零依赖。
 *
 * 注意：rich-text 的 nodes 字符串只支持受信任标签子集（div/p/span/ul/ol/li/
 * strong/em/br/code 等），不支持 view、pre 等小程序组件标签——真机 WebView
 * 遇到不支持的标签会静默渲染为空白，因此这里严格只用受信任标签。
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
  // 粗体、斜体
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  out = out.replace(/__([^_]+)__/g, '<strong>$1</strong>')
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
  // 链接 [text](url)
  out = out.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<span class="md-link">$1</span>')
  return out
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

  for (const rawLine of lines) {
    const line = rawLine
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

    const heading = trimmed.match(/^(#{1,4})\s+(.*)$/)
    if (heading) {
      flushPara(); closeList()
      const level = heading[1].length
      html.push(`<p class="md-h md-h${level}">${renderInline(heading[2])}</p>`)
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
    para.push(line)
  }
  flushPara(); closeList()
  if (inCode) flushCode()
  // 无有效块时返回空串，让调用方（item.html || item.content）回退到纯文本
  if (!html.length) return ''
  return `<div class="md">${html.join('')}</div>`
}
