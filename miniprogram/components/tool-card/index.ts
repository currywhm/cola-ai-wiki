import { localizeImage } from '../../services/media'

type JsonMap = Record<string, any>

interface DiffLine {
  key: number
  kind: 'add' | 'remove' | 'context'
  text: string
  oldNo: string
  newNo: string
}

interface DiffFile {
  path: string
  added: number
  removed: number
  lines: DiffLine[]
  truncated: boolean
}

function mapOf(value: any): JsonMap {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
}

function listOf(value: any): any[] {
  return Array.isArray(value) ? value : []
}

function textOf(value: any, empty = ''): string {
  return value === undefined || value === null ? empty : String(value)
}

function statusLabel(status: string): string {
  const value = status.toLowerCase()
  if (value === 'completed' || value === 'done') return '已完成'
  if (value === 'in_progress' || value === 'running') return '进行中'
  if (value === 'cancelled' || value === 'canceled') return '已取消'
  return '待处理'
}

function diffLines(oldText: string, newText: string): { lines: DiffLine[]; truncated: boolean } {
  const oldLines = String(oldText || '').split('\n').map((line) => line.slice(0, 800))
  const newLines = String(newText || '').split('\n').map((line) => line.slice(0, 800))
  if (oldLines.length > 160 || newLines.length > 160) {
    const lines: DiffLine[] = []
    oldLines.slice(0, 160).forEach((line, index) => lines.push({ key: index, kind: 'remove', text: line, oldNo: String(index + 1), newNo: '' }))
    newLines.slice(0, 160).forEach((line, index) => lines.push({ key: 1000 + index, kind: 'add', text: line, oldNo: '', newNo: String(index + 1) }))
    return { lines, truncated: oldLines.length > 160 || newLines.length > 160 }
  }
  const rows = oldLines.length + 1
  const cols = newLines.length + 1
  const dp: number[][] = Array.from({ length: rows }, () => new Array<number>(cols).fill(0))
  for (let i = oldLines.length - 1; i >= 0; i -= 1) {
    for (let j = newLines.length - 1; j >= 0; j -= 1) {
      dp[i][j] = oldLines[i] === newLines[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const lines: DiffLine[] = []
  let i = 0
  let j = 0
  let key = 0
  while (i < oldLines.length || j < newLines.length) {
    if (i < oldLines.length && j < newLines.length && oldLines[i] === newLines[j]) {
      lines.push({ key, kind: 'context', text: oldLines[i], oldNo: String(i + 1), newNo: String(j + 1) })
      i += 1
      j += 1
    } else if (j < newLines.length && (i >= oldLines.length || dp[i][j + 1] >= dp[i + 1][j])) {
      lines.push({ key, kind: 'add', text: newLines[j], oldNo: '', newNo: String(j + 1) })
      j += 1
    } else {
      lines.push({ key, kind: 'remove', text: oldLines[i], oldNo: String(i + 1), newNo: '' })
      i += 1
    }
    key += 1
  }
  return { lines, truncated: false }
}

function diffFiles(node: JsonMap): DiffFile[] {
  const resultMeta = mapOf(node.resultMeta)
  const callMeta = mapOf(node.toolMeta)
  let source = listOf(resultMeta.diffs)
  if (!source.length && node.variant === 'edit') {
    source = [{ path: callMeta.path, old_text: callMeta.old_text, new_text: callMeta.new_text }]
  }
  if (!source.length && node.variant === 'write') {
    source = [{ path: callMeta.path, old_text: null, new_text: callMeta.content }]
  }
  return source.slice(0, 20).map((item: any) => {
    const value = mapOf(item)
    const oldText = value.old_text === null || value.old_text === undefined ? '' : textOf(value.old_text)
    const newText = textOf(value.new_text)
    const diff = diffLines(oldText, newText)
    return {
      path: textOf(value.path, '文件'),
      added: diff.lines.filter((line) => line.kind === 'add').length,
      removed: diff.lines.filter((line) => line.kind === 'remove').length,
      lines: diff.lines.slice(0, 280),
      truncated: diff.truncated || diff.lines.length > 280,
    }
  })
}

function terminalView(node: JsonMap): JsonMap {
  const meta = mapOf(node.toolMeta)
  const output = textOf(node.toolOutput)
  const signal = textOf(mapOf(node.resultMeta).signal)
  const exitCode = Number(mapOf(node.resultMeta).exit_code)
  return {
    command: textOf(meta.command, node.summary || ''),
    cwd: textOf(meta.cwd),
    description: textOf(meta.description),
    output,
    hasOutput: !!output,
    exitText: signal ? `信号 ${signal}` : (Number.isFinite(exitCode) && exitCode !== 0 ? `退出码 ${exitCode}` : ''),
  }
}

function searchView(node: JsonMap): JsonMap {
  const meta = mapOf(node.resultMeta)
  const sources = listOf(meta.sources).map((item: any) => {
    const source = mapOf(item)
    return {
      title: textOf(source.title, textOf(source.url, '网页来源')),
      url: textOf(source.url),
      snippet: textOf(source.snippet),
      publishedAt: textOf(source.published_at),
    }
  })
  const files = listOf(meta.files).map((item: any) => {
    const file = mapOf(item)
    return {
      path: textOf(file.path, '文件'),
      matches: listOf(file.matches).map((match: any) => {
        const value = mapOf(match)
        return { lineNumber: Number(value.line_number || 0), line: textOf(value.line) }
      }),
    }
  })
  const paths = listOf(meta.paths).map((path) => textOf(path)).filter(Boolean)
  const total = Number(meta.total || sources.length || files.reduce((sum: number, file: any) => sum + file.matches.length, paths.length))
  return {
    sources,
    files,
    paths,
    total: Number.isFinite(total) ? total : 0,
    truncated: meta.truncated === true,
    hasSources: sources.length > 0,
    hasFiles: files.length > 0,
    hasPaths: paths.length > 0,
  }
}

function readView(node: JsonMap): JsonMap {
  const meta = mapOf(node.resultMeta)
  const callMeta = mapOf(node.toolMeta)
  const isFetch = meta.kind === 'fetch'
  const lines = listOf(meta.lines).map((item: any) => {
    const line = mapOf(item)
    return { number: Number(line.number || 0), text: textOf(line.text) }
  })
  return {
    isFetch,
    path: isFetch ? textOf(meta.url, textOf(callMeta.url, node.summary || '')) : textOf(meta.path, textOf(callMeta.path, node.summary || '')),
    offset: Number(meta.offset || callMeta.offset || 1),
    totalLines: Number(meta.total_lines || 0),
    lang: textOf(meta.lang),
    statusCode: Number(meta.status_code || 0),
    statusText: Number(meta.status_code || 0) ? `HTTP ${Number(meta.status_code)}` : '',
    truncated: meta.truncated === true,
    lines,
    hasLines: lines.length > 0,
    fallback: textOf(node.toolOutput),
  }
}

function imageView(node: JsonMap): JsonMap {
  const images = listOf(mapOf(node.resultMeta).images).map((item: any, index: number) => {
    const image = mapOf(item)
    const width = Number(image.width || 0)
    const height = Number(image.height || 0)
    const bytes = Number(image.bytes || 0)
    return {
      key: textOf(image.id, `image-${index}`),
      name: textOf(image.name, `图片 ${index + 1}`),
      mediaType: textOf(image.media_type, 'image'),
      size: bytes ? `${Math.max(1, Math.round(bytes / 1024))} KB` : '',
      dimensions: width && height ? `${width} × ${height}` : '',
      src: '',
    }
  })
  return { images, hasImages: images.length > 0 }
}

function todoView(node: JsonMap): JsonMap {
  const items = listOf(mapOf(node.toolMeta).todos).map((item: any, index: number) => {
    const todo = mapOf(item)
    return {
      key: index,
      content: textOf(todo.content, '未命名任务'),
      status: textOf(todo.status, 'pending'),
      statusLabel: statusLabel(textOf(todo.status, 'pending')),
      active: todo.active === true,
    }
  })
  const done = items.filter((item) => item.status === 'completed' || item.status === 'done').length
  return { items, done, total: items.length }
}

function genericView(node: JsonMap): JsonMap {
  const input = textOf(node.toolInput)
  const output = textOf(node.toolOutput)
  const error = textOf(node.toolError)
  return { input, output, error, hasInput: !!input, hasOutput: !!output, hasError: !!error }
}

Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    node: { type: Object, value: {} },
  },
  data: {
    open: false,
    view: {
      variant: 'generic',
      summary: '',
      terminal: {},
      search: {},
      read: {},
      images: [],
      todos: {},
      generic: {},
      diffs: [],
    },
  },
  observers: {
    node(value: any) {
      this.sync(value || {})
    },
  },
  lifetimes: {
    attached() {
      this.sync(this.data.node || {})
    },
  },
  methods: {
    sync(value: any) {
      const node = mapOf(value)
      const variant = textOf(node.variant, 'generic')
      const summary = node.state === 'error'
        ? textOf(node.toolError, textOf(node.resultSummary, textOf(node.summary)))
        : textOf(node.resultSummary, textOf(node.summary, textOf(node.detail)))
      const view: any = {
        variant,
        summary,
        terminal: terminalView(node),
        search: searchView(node),
        read: readView(node),
        images: imageView(node).images,
        todos: todoView(node),
        generic: genericView(node),
        diffs: diffFiles(node),
      }
      view.expandable = !!(summary || view.terminal.hasOutput || view.generic.hasInput || view.generic.hasOutput || view.generic.hasError || view.read.hasLines || view.search.hasSources || view.search.hasFiles || view.search.hasPaths || view.images.length || view.todos.items.length || view.diffs.length)
      this.setData({ view })
      if (variant === 'image') this.localizeImages(view.images)
    },
    localizeImages(images: any[]) {
      if (!images.some((image) => !!image.src)) return
      const token = Date.now()
      ;(this as any).imageToken = token
      Promise.all(images.map((image) => image.src ? localizeImage(image.src).then((src) => ({ ...image, src })) : Promise.resolve(image))).then((next) => {
        if ((this as any).imageToken !== token) return
        const view = { ...(this.data.view as any), images: next }
        this.setData({ view })
      }).catch(() => undefined)
    },
    toggle() {
      const expandable = (this.data.view as any).summary
        || (this.data.view as any).terminal?.hasOutput
        || (this.data.view as any).generic?.hasInput
        || (this.data.view as any).generic?.hasOutput
        || (this.data.view as any).generic?.hasError
        || (this.data.view as any).read?.hasLines
        || (this.data.view as any).search?.hasSources
        || (this.data.view as any).search?.hasFiles
        || (this.data.view as any).search?.hasPaths
        || (this.data.view as any).images?.length
        || (this.data.view as any).todos?.items?.length
        || (this.data.view as any).diffs?.length
      if (!expandable) return
      this.setData({ open: !this.data.open })
    },
    copySection(e: any) {
      const kind = textOf((e.currentTarget.dataset || {}).kind)
      const view = this.data.view as any
      let content = ''
      if (kind === 'input') content = view.generic?.input || ''
      else if (kind === 'output') content = view.generic?.output || view.read?.fallback || view.terminal?.output || ''
      else if (kind === 'terminal') content = view.terminal?.output || ''
      else if (kind === 'diff') content = (view.diffs || []).map((file: any) => `${file.path}\n${file.lines.map((line: any) => `${line.kind === 'add' ? '+' : line.kind === 'remove' ? '-' : ' '}${line.text}`).join('\n')}`).join('\n\n')
      if (!content) return
      wx.setClipboardData({ data: content, success: () => wx.showToast({ title: '已复制', icon: 'none' }) })
    },
    previewImage(e: any) {
      const src = textOf((e.currentTarget.dataset || {}).src)
      if (!src) return
      wx.previewImage({ current: src, urls: [src] })
    },
  },
})
