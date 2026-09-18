// 会话线程共用逻辑：把 harness 推来的 思考 / 步骤 / 工具 / 技能 / 子智能体 事件
// 折叠成「过程区」，并负责一轮结束后的收尾与增量文本节流。
// 「问AI」独立问答页、知识库会话、文件夹会话共用这一份实现，
// 避免三个入口各写一遍导致的表现不一致。

import { renderMarkdown } from './markdown'
import { fileIconName } from './file-type'
import { artifactIcon, artifactMeta } from './artifact'

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
  /** 分组层级：0=step 容器或独立节点，1=挂在「第 N 步 · 动作」下面的具体调用 */
  depth?: number
  /** 工具调用标识；同一调用的 run/result 靠它合并，不靠可能重复的工具名。 */
  callId?: string
  name?: string
  variant?: string
  summary?: string
  toolInput?: string
  toolMeta?: any
  toolOutput?: string
  toolError?: string
  resultSummary?: string
  resultMeta?: any
  durationMs?: number
  durationLabel?: string
  /** 计划模式：kind==='plan' 时携带完整计划 markdown（页面附着卡片展示） */
  plan?: string
  /** 计划正文渲染后的 HTML（rich-text 使用） */
  planHtml?: string
  /** 评审 id（= 该轮 harness 会话 id），用户批准时回传后端 */
  reviewId?: string
  /** review=待确认 / approved=已批准 / revising=继续规划 / error=评审失败 */
  reviewState?: string
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

function toolDurationLabel(durationMs?: number): string {
  const ms = Number(durationMs || 0)
  if (!ms) return ''
  if (ms < 1000) return '<1 秒'
  const seconds = Math.round(ms / 1000)
  return seconds < 60 ? `${seconds} 秒` : `${Math.round(seconds / 60)} 分钟`
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

// 摘要行去掉 markdown 的加粗标记，避免折叠行里露出一串 **
function plain(text: string): string {
  return String(text || '').replace(/\*\*/g, '')
}

/** 现有 step 容器里最大的步号，用于后端没带 index 时兜底 */
function lastStepIndex(trace: TraceNode[]): number {
  return trace.reduce((max, node) => {
    if (node.kind !== 'step') return max
    return Math.max(max, Number(String(node.key).replace('step-', '')) || 0)
  }, 0)
}

/**
 * 抹掉「下面什么都没有」的步骤行。历史对话里存过旧版后端发来的空壳
 * （标题只有「第 2 步」、下面一行内容也没有），渲染出来只会让人困惑。
 * 带动作名的分组标题（「第 1 步 · 检索知识库」）和有子调用的分组一律保留。
 */
function dropEmptySteps(trace: TraceNode[]): TraceNode[] {
  return trace.filter((node, index) => {
    if (node.kind !== 'step') return true
    if (!/^第 \d+ 步$/.test(String(node.title || ''))) return true
    const next = trace[index + 1]
    return !!next && next.depth === 1
  })
}

/** 当前仍然开着的那一步（工具节点据此判断要不要缩进一级） */
function openStepNode(trace: TraceNode[]): TraceNode | null {
  for (let idx = trace.length - 1; idx >= 0; idx -= 1) {
    const node = trace[idx]
    if (node.kind === 'step') return node.state === 'run' ? node : null
  }
  return null
}

// ---- 参考出处图标：PDF / Word / 表格 / 图片 / 公开网页 ----
// 图标映射统一在 utils/file-type 里，知识库列表 / 最近 / 参考出处 / 对话产物共用一套。

/** 给参考出处补上类型图标，供 wxml 直接渲染 */
export function decorateSources(sources: any[] | undefined): any[] {
  return (sources || []).map((source: any) => {
    if (source && source.iconName) return source
    if (source && source.url) return { ...source, iconName: 'quanwang' }
    const name = String((source && (source.filename || source.name)) || '').toLowerCase()
    const hit = /\.[a-z0-9]+$/.exec(name)
    return { ...source, iconName: hit ? fileIconName(hit[0]) : 'recent-text' }
  })
}

/** 把一条 trace 事件并入目标消息；事件与消息无关时原样返回，避免多余 setData。 */
// ---- 对话产物（agent 本轮生成的文件）----

/**
 * 给产物补上「类型图标 + 一行副解读物」。
 * 后端只给事实（名称 / 后缀 / 体积 / 是否已进知识库），怎么展示由这里统一决定，
 * 实时流与历史回放因此长得完全一样。
 */
export function decorateArtifacts(items: any[] | undefined): any[] {
  return (items || []).filter((item: any) => item && item.id).map((item: any) => ({
    ...item,
    icon: item.icon || artifactIcon(item),
    meta: item.meta || artifactMeta(item),
  }))
}

/** 把后端推来的产物并入目标消息（同一 id 只留一条，断线重连不会出两张卡） */
export function addArtifact(messages: any[], id: string, artifact: any): TraceResult {
  const index = messages.findIndex((message: any) => message.id === id)
  if (index < 0 || !artifact || !artifact.id) return { messages, changed: false }
  const existing: any[] = messages[index].artifacts || []
  if (existing.some((item: any) => item.id === artifact.id)) return { messages, changed: false }
  const next = messages.slice()
  next[index] = { ...messages[index], artifacts: decorateArtifacts([...existing, artifact]) }
  return { messages: next, changed: true }
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
    // step 是「一段执行」的分组容器：后端只在这一步真的调了工具时才发过来，
    // 标题里已经带上动作（「第 1 步 · 检索知识库」），不会再出现下面空无一物的步骤行。
    const index = Number(item.index) || lastStepIndex(trace) + 1
    const key = `step-${index}`
    const state = String(item.state || 'run')
    const at = trace.findIndex((node) => node.key === key)
    // 收尾事件只带 state 不带标题，沿用开容器时那句动作
    const title = item.title || (at >= 0 ? trace[at].title : `第 ${index} 步`)
    const node: TraceNode = { key, kind: 'step', state, title, icon: nodeIcon('step'), stateLabel: '', depth: 0 }
    if (at >= 0) trace[at] = { ...trace[at], ...node }
    else trace.push(node)
  } else if (item.kind === 'tool' || item.kind === 'skill' || item.kind === 'agent') {
    const state = String(item.state || 'done')
    const callId = String(item.call_id || '')
    const toolName = String(item.name || '')
    // 工具必须按 callId 合并：并行同名调用不能用工具名做 key。
    const key = item.kind === 'tool' && callId
      ? `tool:${callId}`
      : `${item.kind}:${toolName || item.title || trace.length}`
    let at = trace.findIndex((node) => node.key === key || (item.kind === 'tool' && callId && node.callId === callId))
    // 旧事件没有 callId 时，只回并最后一个同类型未完成节点；结果事件尽量保持原 key。
    if (at < 0 && (state === 'done' || state === 'error')) {
      for (let idx = trace.length - 1; idx >= 0; idx -= 1) {
        const candidate = trace[idx]
        if (candidate.kind !== item.kind || (candidate.state !== 'run' && candidate.state !== 'load')) continue
        if (item.kind === 'tool' && toolName && candidate.name && candidate.name !== toolName) continue
        at = idx
        break
      }
    }
    const durationMs = Number(item.duration_ms || 0)
    const node: TraceNode = {
      key,
      kind: item.kind,
      state,
      title: item.title || '',
      detail: item.detail || '',
      icon: nodeIcon(item.kind, toolName),
      stateLabel: nodeStateLabel(state),
      callId,
      name: toolName,
      variant: String(item.tool_variant || ''),
      summary: String(item.tool_result_summary || item.tool_summary || item.detail || ''),
      toolInput: String(item.tool_input || ''),
      toolMeta: item.tool_meta || {},
      toolOutput: String(item.tool_output || ''),
      toolError: String(item.tool_error || ''),
      resultSummary: String(item.tool_result_summary || ''),
      resultMeta: item.tool_result_meta || {},
      durationMs: durationMs || undefined,
      durationLabel: toolDurationLabel(durationMs),
    }
    if (at >= 0) {
      const prev = trace[at]
      const title = state === 'done' && prev.title ? prev.title : (node.title || prev.title)
      const icon = toolName ? nodeIcon(item.kind, toolName) : (prev.icon || node.icon)
      trace[at] = {
        ...prev,
        ...node,
        key: prev.key || key,
        title,
        icon,
        name: toolName || prev.name || '',
        variant: node.variant || prev.variant || '',
        detail: node.detail || prev.detail || '',
        summary: node.summary || prev.summary || '',
        toolInput: node.toolInput || prev.toolInput || '',
        toolMeta: node.toolMeta && Object.keys(node.toolMeta).length ? node.toolMeta : (prev.toolMeta || {}),
        toolOutput: node.toolOutput || prev.toolOutput || '',
        toolError: node.toolError || prev.toolError || '',
        resultSummary: node.resultSummary || prev.resultSummary || '',
        resultMeta: node.resultMeta && Object.keys(node.resultMeta).length ? node.resultMeta : (prev.resultMeta || {}),
        durationMs: node.durationMs || prev.durationMs,
        durationLabel: node.durationLabel || prev.durationLabel || '',
        depth: prev.depth || 0,
      }
    } else {
      node.depth = openStepNode(trace) ? 1 : 0
      trace.push(node)
    }
  } else if (item.kind === 'plan') {
    // 计划模式：官方 exit_plan_mode 提交的完整计划。以「页面附着卡片」呈现——
    // 它不进过程区的步骤流，折叠过程区也依然可见，因此不影响对话观感与使用。
    const reviewId = String(item.review_id || '')
    const key = `plan:${reviewId}`
    const state = String(item.state || 'review')
    let at = trace.findIndex((node) => node.kind === 'plan' && (reviewId === '' || node.key === key))
    if (at < 0 && state !== 'review') {
      for (let idx = trace.length - 1; idx >= 0; idx -= 1) {
        if (trace[idx].kind === 'plan') { at = idx; break }
      }
    }
    const plan = String(item.plan || (at >= 0 ? trace[at].plan || '' : ''))
    const node: TraceNode = {
      key, kind: 'plan',
      state: state === 'review' ? 'run' : (state === 'error' ? 'error' : 'done'),
      // 标题始终是计划自己的名字：批准/继续规划只反映在副标题与状态字上，
      // 避免「批准并执行」这类系统文案顶掉用户正在读的标题。
      title: (at >= 0 && trace[at].title && state !== 'review')
        ? trace[at].title
        : (item.title || (at >= 0 ? trace[at].title : '') || '执行计划'),
      detail: '',
      icon: 'agent-mode',
      stateLabel: state === 'review' ? '待确认' : (state === 'error' ? '继续规划' : '已批准'),
      plan,
      planHtml: plan ? renderMarkdown(plan) : '',
      reviewId: reviewId || (at >= 0 ? trace[at].reviewId : '') || '',
      reviewState: state,
    }
    if (at >= 0) trace[at] = { ...trace[at], ...node, plan, planHtml: node.planHtml || trace[at].planHtml }
    else trace.push(node)
  } else if (item.kind === 'note') {
    // 提示类节点只保留最新一条，避免网络重试等信息堆叠
    trace = trace.filter((node) => node.kind !== 'note')
    const state = String(item.state || 'done')
    trace.push({ key: 'note', kind: 'note', state, title: item.title || '', detail: '', icon: nodeIcon('note'), stateLabel: nodeStateLabel(state) })
  } else {
    return { messages, changed: false }
  }

  // 折叠行的摘要：优先显示当前动作（“检索全网资料 · 中粮集团…”），否则跟到最后一行思考。
  // 思考全文只在用户展开那一行时才渲染，不会再一上来就铺满一屏推理文字。
  const latest = trace[trace.length - 1]
  const action = latest && latest.kind !== 'step' ? (latest.detail ? `${latest.title} · ${latest.detail}` : latest.title) : ''
  const next = messages.slice()
  const planNode = planNodeOf(trace)
  next[index] = {
    ...message,
    trace,
    reason,
    running: true,
    // 用户自己展开过就尊重他的选择，默认保持折叠
    reasonOpen: message.reasonOpen === true,
    traceTitle: '思考中',
    traceIcon: 'dengpao',
    traceSummary: plain(action || lastLine(reason)),
    // 计划卡片默认展开（评审需要立刻看到计划正文），展开态是消息自己的状态
    planNode,
    planOpen: message.planOpen === undefined ? true : message.planOpen,
  }
  return { messages: next, changed: true }
}

/** 一轮结束：收起过程区，标题换成「已完成思考 · N 个步骤」。 */

/** 取最后一条计划节点（计划卡片只认最新一次评审）。 */
function planNodeOf(trace: TraceNode[]) {
  for (let idx = trace.length - 1; idx >= 0; idx -= 1) {
    if (trace[idx].kind === 'plan') return trace[idx]
  }
  return null
}

/**
 * 用户在计划卡片上做出选择后的本地收尾：先把按钮收起，避免重复提交；
 * 运行时的权威状态会随后通过 plan 节点（approved / error）合并回来。
 */
export function markPlanReviewed(messages: any[], id: string, reviewState: string) {
  const labels: Record<string, { state: string; label: string }> = {
    review: { state: 'run', label: '待确认' },
    approved: { state: 'done', label: '已批准' },
    revising: { state: 'run', label: '继续规划中' },
  }
  const view = labels[reviewState] || labels.review
  return messages.map((message: any) => {
    if (message.id !== id || !message.planNode) return message
    return {
      ...message,
      planNode: {
        ...message.planNode,
        state: view.state,
        stateLabel: view.label,
        reviewState,
      },
    }
  })
}

/** 展开 / 收起计划卡片。 */
export function togglePlan(messages: any[], id: string) {
  return messages.map((message: any) => (
    message.id === id && message.planNode ? { ...message, planOpen: !message.planOpen } : message
  ))
}

function elapsedLabel(startedAt?: number): string {
  if (!startedAt) return ''
  const seconds = Math.round((Date.now() - startedAt) / 1000)
  if (seconds < 1) return ''
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.round(seconds / 60)} 分钟`
}

/**
 * 一轮结束：思考折叠成一行摘要，标题从「思考中」换成「已深度思考」。
 * 与 harness 的 ReasoningRow 一致：折叠时只留思考的第一行，展开才看全文，
 * 不会再一屏铺满灰色推理文字；步骤行本来就只有真的调了工具才会出现，这里不动它们。
 * 顺带把没收尾的节点补成完成态，避免流中断后一直停在「进行中」。
 */
export function settleTrace(messages: any[], id: string): any[] {
  return messages.map((message: any) => {
    if (message.id !== id || !message.trace) return message
    const trace = (message.trace || []).map((node: any) => (
      node.state === 'run' || node.state === 'load' ? { ...node, state: 'done', stateLabel: '' } : node
    ))
    const reason = String(message.reason || '')
    const summary = plain(firstLine(reason))
    const kept = dropEmptySteps(trace)
    return {
      ...message,
      trace: kept,
      running: false,
      traceTitle: summary ? '已深度思考' : '已完成',
      traceIcon: summary ? 'dui' : 'dengpao',
      traceSummary: summary,
      traceElapsed: elapsedLabel(message.traceStartedAt),
      planNode: planNodeOf(kept),
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
    // 工具产物（agent 生成的文件）：后端随时推来，随消息一起落库、一起回放
    artifacts: [] as any[],
    trace: [],
    reason: '',
    // 思考默认折叠成一行摘要，用户展开才看全文
    reasonOpen: false,
    running: true,
    traceTitle: '思考中',
    traceIcon: 'dengpao',
    traceSummary: '',
    traceElapsed: '',
    // 计时起点：结束后用来显示「已深度思考 · 3 秒」，跟 harness 的耗时提示对齐
    traceStartedAt: Date.now(),
  }
}

/** 服务端存下的耗时转成和实时结束一致的文案。 */
function durationLabel(durationMs?: number): string {
  const seconds = Math.round(Number(durationMs || 0) / 1000)
  if (!seconds) return ''
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.round(seconds / 60)} 分钟`
}

/**
 * 历史对话回放：后端把思考全文、过程节点、耗时随消息一起落了库，
 * 这里用与实时流完全相同的 appendTrace / settleTrace 还原，
 * 保证「重新进入对话」和「刚回答完」看到的过程区、折叠状态、耗时、排版一致。
 * 旧数据没有这些字段时降级为只有正文，不会多出空的过程区。
 */
export function hydrateAssistant(message: any): any {
  const items: any[] = Array.isArray(message.trace) ? message.trace : []
  let draft: any[] = [{
    ...message,
    trace: [],
    reason: String(message.reason || ''),
    reasonOpen: false,
    running: true,
    traceTitle: '思考中',
    traceSummary: '',
    traceElapsed: '',
  }]
  items.forEach((item) => {
    const result = appendTrace(draft, message.id, item)
    if (result.changed) draft = result.messages
  })
  const restored = settleTrace(draft, message.id)[0]
  return {
    ...restored,
    html: renderMarkdown(message.content || ''),
    sources: decorateSources(message.sources),
    artifacts: decorateArtifacts(message.artifacts),
    traceElapsed: durationLabel(message.duration_ms),
    // 计划卡片随过程节点一起落库：重进对话时同样复原，只是默认收起
    planNode: restored.planNode || null,
    planOpen: false,
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
