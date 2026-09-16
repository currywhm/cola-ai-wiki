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
}

export interface TraceResult {
  messages: any[]
  changed: boolean
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
      trace.push({ key, kind: 'step', state: 'done', title: item.title || `第 ${item.index || trace.length + 1} 步` })
    }
  } else if (item.kind === 'tool' || item.kind === 'skill' || item.kind === 'agent') {
    // 同一动作先 run 后 done：按 key 合并状态，避免出现两条重复节点
    const key = `${item.kind}:${item.name || item.title || ''}`
    const at = trace.findIndex((node) => node.key === key)
    const node: TraceNode = { key, kind: item.kind, state: item.state || 'done', title: item.title || '', detail: item.detail || '' }
    if (at >= 0) trace[at] = { ...trace[at], ...node, title: node.title || trace[at].title, detail: node.detail || trace[at].detail }
    else trace.push(node)
  } else if (item.kind === 'note') {
    // 提示类节点只保留最新一条，避免网络重试等信息堆叠
    trace = trace.filter((node) => node.kind !== 'note')
    trace.push({ key: 'note', kind: 'note', state: item.state || 'done', title: item.title || '' })
  } else {
    return { messages, changed: false }
  }

  const latest = trace[trace.length - 1]
  const busy = !!latest && (latest.kind === 'step' || latest.state === 'run' || latest.state === 'load')
  const next = messages.slice()
  next[index] = {
    ...message,
    trace,
    reason,
    running: true,
    traceOpen: true,
      traceTitle: busy && latest.kind !== 'step' ? `${latest.title}…` : '正在思考',
  }
  return { messages: next, changed: true }
}

/** 一轮结束：收起过程区，标题换成「已完成思考 · N 个步骤」。 */
export function settleTrace(messages: any[], id: string): any[] {
  return messages.map((message: any) => {
    if (message.id !== id || !message.trace) return message
    const actions = (message.trace || []).filter((node: any) => node.kind === 'tool' || node.kind === 'skill' || node.kind === 'agent').length
    const hasTrace = actions > 0 || !!message.reason
    const title = actions > 0 ? `已完成思考，${actions} 个步骤` : '已完成思考'
    return { ...message, running: false, traceOpen: !hasTrace, traceTitle: hasTrace ? title : '正在思考' }
  })
}

/** 新增一条回答占位：带上过程区需要的初始字段。 */
export function assistantMessage(id: string) {
  return { id, role: 'assistant', content: '', html: '', sources: [], trace: [], reason: '', traceOpen: true, running: true, traceTitle: '正在思考' }
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
