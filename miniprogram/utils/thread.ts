// 会话线程共用逻辑：把 harness 推来的 思考 / 步骤 / 工具 / 技能 / 子智能体 事件
// 折叠成「过程区」，并负责一轮结束后的收尾与增量文本节流。
// 「问AI」独立问答页、知识库会话、文件夹会话共用这一份实现，
// 避免三个入口各写一遍导致的表现不一致。

export interface TraceNode {
  key: string
  kind: string
  state: string
  title: string
  detail?: string
  /** 节点图标名，对应 assets/icons/cola-set 里的图标 */
  icon: string
  /** 右侧状态字：只有「进行中 / 失败」才有值，完成的动作不啰嗦 */
  stateLabel: string
}

export interface TraceResult {
  messages: any[]
  changed: boolean
}

// ---- 过程区图标：按「这一步在做什么」选图，避免所有节点长得一模一样 ----

const TOOL_ICONS: Record<string, string> = {
  web_search: 'sousuo',
  web_fetch: 'wangluo',
  bash: 'sliders',
  read: 'book',
  write: 'upload',
  edit: 'upload',
  grep: 'sousuo',
  glob: 'liebiao',
  read_image: 'image',
  subagent: 'robot',
  subagent_fork: 'robot',
  send_message: 'at',
  todo_write: 'liebiao',
  create_goal: 'dengpao',
  update_goal: 'dengpao',
  get_goal: 'dengpao',
  workflow: 'agent-mode',
  plan: 'agent-mode',
  exit_plan_mode: 'dui',
}

const KIND_ICONS: Record<string, string> = { reason: 'dengpao', step: 'liebiao', skill: 'skill-node', agent: 'robot', note: 'jieshao' }

function nodeIcon(kind: string, name?: string): string {
  if (kind === 'tool' && name && TOOL_ICONS[name]) return TOOL_ICONS[name]
  return KIND_ICONS[kind] || 'atom'
}

function nodeStateLabel(state: string): string {
  if (state === 'run' || state === 'load') return '进行中'
  if (state === 'error') return '失败'
  return ''
}

function visibleLines(text: string): string[] {
  return String(text || '').trim().split('\n').map((line) => line.trim()).filter(Boolean)
}

function lastLine(text: string): string {
  const lines = visibleLines(text)
  return lines.length ? lines[lines.length - 1] : ''
}

function firstLine(text: string): string {
  const lines = visibleLines(text)
  return lines.length ? lines[0] : ''
}

// ---- 参考出处图标：PDF / Word / 表格 / 图片 / 公开网页 ----

const SOURCE_SUFFIX_ICONS: Array<[string, string]> = [
  ['.pdf', 'recent-pdf'],
  ['.doc', 'recent-doc'],
  ['.docx', 'recent-doc'],
  ['.md', 'recent-text'],
  ['.txt', 'recent-text'],
  ['.ppt', 'recent-ppt'],
  ['.pptx', 'recent-ppt'],
  ['.xls', 'recent-sheet'],
  ['.xlsx', 'recent-sheet'],
  ['.csv', 'recent-sheet'],
  ['.png', 'recent-image'],
  ['.jpg', 'recent-image'],
  ['.jpeg', 'recent-image'],
  ['.webp', 'recent-image'],
  ['.gif', 'recent-image'],
]

/** 给参考出处补上类型图标，供 wxml 直接渲染 */
export function decorateSources(sources: any[] | undefined): any[] {
  return (sources || []).map((source: any) => {
    if (source && source.iconName) return source
    if (source && source.url) return { ...source, iconName: 'quanwang' }
    const name = String((source && (source.filename || source.name)) || '').toLowerCase()
    const hit = SOURCE_SUFFIX_ICONS.find(([suffix]) => name.endsWith(suffix))
    return { ...source, iconName: hit ? hit[1] : 'recent-text' }
  })
}

/** 把一条 trace 事件并入目标消息；事件与消息无关时原样返回，避免多余 setData。 */
export function appendTrace(messages: any[], id: string, item: any): TraceResult {
  const index = messages.findIndex((message: any) => message.id === id)
  if (index < 0) return { messages, changed: false }
  const message = messages[index]
  let trace: TraceNode[] = (message.trace || []).slice()
  let reason = message.reason || ''

  if (item.kind === 'reason') {
    // 思考增量只有文字，累加到过程区顶部
    reason += item.text || ''
  } else if (item.kind === 'step') {
    const key = `step-${item.index || trace.length + 1}`
    if (!trace.some((node) => node.key === key)) {
      trace.push({ key, kind: 'step', state: 'done', title: item.title || `第 ${item.index || trace.length + 1} 步`, icon: nodeIcon('step'), stateLabel: '' })
    }
  } else if (item.kind === 'tool' || item.kind === 'skill' || item.kind === 'agent') {
    // 同一动作先 run 后 done：按 key 合并状态，避免出现两条重复节点
    const state = String(item.state || 'done')
    const key = `${item.kind}:${item.name || item.title || ''}`
    let at = trace.findIndex((node) => node.key === key)
    // 结束事件不一定带 name（技能：run 是「加载技能 · X」、done 是「技能已加载」，
    // 子智能体同理），key 对不上时回退到同类型最后一个未结束的节点合并，避免多出一行
    if (at < 0 && (state === 'done' || state === 'error')) {
      for (let idx = trace.length - 1; idx >= 0; idx -= 1) {
        if (trace[idx].kind === item.kind && (trace[idx].state === 'run' || trace[idx].state === 'load')) { at = idx; break }
      }
    }
    const node: TraceNode = { key, kind: item.kind, state, title: item.title || '', detail: item.detail || '', icon: nodeIcon(item.kind, item.name), stateLabel: nodeStateLabel(state) }
    if (at >= 0) {
      const prev = trace[at]
      // 动作结束后沿用动作本身的标题（「检索全网资料」而不是「检索全网资料完成」），
      // 是否结束交给右侧状态字；图标也保留 run 那次选中的，不因缺 name 而回退
      const title = state === 'done' && prev.title ? prev.title : (node.title || prev.title)
      const icon = item.name ? nodeIcon(item.kind, item.name) : (prev.icon || node.icon)
      trace[at] = { ...prev, ...node, title, icon, detail: node.detail || prev.detail }
    } else trace.push(node)
  } else if (item.kind === 'note') {
    // 提示类节点只保留最新一条，避免网络重试等信息堆叠
    trace = trace.filter((node) => node.kind !== 'note')
    const state = String(item.state || 'done')
    trace.push({ key: 'note', kind: 'note', state, title: item.title || '', detail: '', icon: nodeIcon('note'), stateLabel: nodeStateLabel(state) })
  } else {
    return { messages, changed: false }
  }

  // 行内摘要：优先显示当前动作（“检索全网资料 · 中粮集团…”），否则跟到最后一行思考
  const latest = trace[trace.length - 1]
  const action = latest && latest.kind !== 'step' ? (latest.detail ? `${latest.title} · ${latest.detail}` : latest.title) : ''
  const next = messages.slice()
  next[index] = {
    ...message,
    trace,
    reason,
    running: true,
    traceOpen: true,
    traceTitle: '思考中',
    traceIcon: (latest && latest.icon) || 'dengpao',
    traceSummary: action || lastLine(reason),
  }
  return { messages: next, changed: true }
}

/** 一轮结束：收起过程区，标题换成「已完成思考 · N 个步骤」。 */

function elapsedLabel(startedAt?: number): string {
  if (!startedAt) return ''
  const seconds = Math.round((Date.now() - startedAt) / 1000)
  if (seconds < 1) return ''
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.round(seconds / 60)} 分钟`
}

/**
 * 一轮结束：收起过程区，标题从「思考中」换成「已深度思考」。
 * 与 harness 的 ReasoningRow 一致：折叠时只留一行摘要（思考的第一行，没有思考就退化成步骤数），
 * 展开才看全文；顺带把没收尾的节点补成完成态，避免流中断后一直停在「进行中」。
 */
export function settleTrace(messages: any[], id: string): any[] {
  return messages.map((message: any) => {
    if (message.id !== id || !message.trace) return message
    const trace = (message.trace || []).map((node: any) => (
      node.state === 'run' || node.state === 'load' ? { ...node, state: 'done', stateLabel: '' } : node
    ))
    const reason = String(message.reason || '')
    const steps = trace.filter((node: any) => node.kind === 'tool' || node.kind === 'skill' || node.kind === 'agent')
    const hasTrace = trace.length > 0 || !!reason
    const summary = firstLine(reason) || (steps.length ? `${steps.length} 个步骤` : '')
    return {
      ...message,
      trace,
      running: false,
      traceOpen: !hasTrace,
      traceTitle: hasTrace ? '已深度思考' : '已完成',
      traceIcon: hasTrace ? 'dui' : 'dengpao',
      traceSummary: summary,
      traceElapsed: elapsedLabel(message.traceStartedAt),
    }
  })
}

/** 新增一条回答占位：带上过程区需要的初始字段。 */
export function assistantMessage(id: string) {
  return {
    id,
    role: 'assistant',
    content: '',
    html: '',
    sources: [],
    trace: [],
    reason: '',
    traceOpen: true,
    running: true,
    traceTitle: '思考中',
    traceIcon: 'dengpao',
    traceSummary: '',
    traceElapsed: '',
    // 计时起点：结束后用来显示「已深度思考 · 3 秒」，跟 harness 的耗时提示对齐
    traceStartedAt: Date.now(),
  }
}

/**
 * 增量文本节流：模型逐字输出时，把 70ms 内的多条增量合并成一次 setData，
 * 兼顾跟手观感与渲染开销（长回答下尤其明显）。
 */
export function createFlusher(apply: (id: string, content: string) => void) {
  let timers: Record<string, () => string> = {}
  let task: any = null

  const flush = () => {
    task = null
    const pending = timers
    timers = {}
    Object.keys(pending).forEach((key) => apply(key, pending[key]()))
  }

  return {
    schedule(id: string, read: () => string) {
      timers[id] = read
      if (!task) task = setTimeout(flush, 70)
    },
    clear(id: string) {
      delete timers[id]
      if (task) {
        clearTimeout(task)
        task = null
      }
    },
    /** 页面卸载时丢弃所有待提交的任务，避免对已销毁页面 setData */
    reset() {
      timers = {}
      if (task) {
        clearTimeout(task)
        task = null
      }
    },
  }
}
