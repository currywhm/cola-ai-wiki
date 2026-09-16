/**
 * @cola/dsh-plan-bridge — 把 dsh 计划模式的「计划评审」接到 cola 小程序后端。
 *
 * 背景：官方 `dsh-plan-mode` 的 `exit_plan_mode` 工具通过 user-questions seam
 * 把计划交给人类评审（`interaction.ask({questions:[{intent:{kind:'plan-review'}}]})`）。
 * 官方 SDK jsonrpc 传输层（`dsh-sdk-jsonrpc-server`）只转发 `session.event` /
 * `session.status`，不转发服务端→客户端的提问请求，因此小程序端拿不到评审通道。
 *
 * 本插件用的就是官方留给部署方的扩展点（`'user-questions/request'` waterfall），
 * 用「文件队列」在后端与运行时之间搭一座桥：
 *
 *   <COLA_BRIDGE_DIR>/<sessionId>.review.json   ← 运行时写入：待评审的计划
 *   <COLA_BRIDGE_DIR>/<sessionId>.answer.json   ← 后端写入：用户的评审结论
 *   <COLA_BRIDGE_DIR>/<sessionId>.mode.json     ← 后端写入：本轮是否进入计划模式
 *
 * 后端（FastAPI）收到 `exit_plan_mode` 的 tool/call 事件后，把计划渲染成
 * 「页面附着卡片」呈现给用户；用户点「批准」或「继续规划」后后端写入 answer 文件，
 * 本插件读到即返回，工具正常返回 `{approved:true}`，计划模式退出、模型开始执行。
 *
 * 超时/中断时按官方约定 **失败并留在计划模式**（抛 ASK_CANCELLED），
 * 绝不静默放行——与 `dsh-plan-mode` 的契约一致。
 *
 * @module @cola/dsh-plan-bridge
 */

import { readFile, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

export const name = 'cola-plan-bridge'

// 计划模式的开关由本插件按后端下发的 mode 文件驱动：官方 SDK 传输层不解析
// `/plan` 命令（命令执行入口只由交互式 UI 载具调用），所以这里直接调官方
// `ctx.planMode.set()`，语义与 `/plan` 完全一致。
export const inject = ['planMode']

/** 计划评审的等待上限：与后端 request_timeout_seconds 配合，留出收尾余量。 */
const DEFAULT_TIMEOUT_MS = 1500000
const POLL_INTERVAL_MS = 400

function bridgeDirOf(config) {
  return String(config?.bridgeDir ?? process.env.COLA_BRIDGE_DIR ?? '').trim()
}

function reviewIdOf(request) {
  const agent = request?.agent
  return String(agent?.session?.id ?? agent?.session?.sessionId ?? '').trim()
}

function planQuestionOf(request) {
  const questions = Array.isArray(request?.questions) ? request.questions : []
  return questions.find(
    (item) =>
      item?.intent?.kind === 'plan-review' &&
      typeof item.detail === 'string' &&
      item.detail.trim() !== '',
  )
}

async function readJson(path) {
  try {
    return JSON.parse(await readFile(path, 'utf8'))
  } catch {
    return null
  }
}

async function removeQuietly(path) {
  try {
    await rm(path, { force: true })
  } catch {
    /* 清理失败不影响评审结论 */
  }
}

export function apply(ctx, config = {}) {
  const bridgeDir = bridgeDirOf(config)
  const timeoutMs = Number(config?.timeoutMs ?? DEFAULT_TIMEOUT_MS)
  const logger = typeof ctx.logger === 'function' ? ctx.logger('cola-plan-bridge') : undefined

  if (bridgeDir === '') {
    // 未配置桥接目录时保持官方默认行为：不接管，交给下游 answerer，
    // 计划评审照旧失败并留在计划模式。
    logger?.warn?.('COLA_BRIDGE_DIR is empty; plan review stays unanswered')
    return
  }

  // 计划模式开关：后端每轮把期望模式写进 <sessionId>.mode.json，这里在步骤边界
  // 一次性消费掉并调用官方 planMode.set（幂等；与当前状态相同即 noop）。
  // dsh-plan-mode 自己的 pre-step 监听器会先 await 下游，因此本插件在其读取
  // pending 之前完成设置，计划指引与当轮请求同一步生效。
  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    const agent = payload?.agent
    if (agent === undefined || decision?.kind === 'reject') return decision
    const sessionId = String(agent.session?.id ?? '').trim()
    if (sessionId === '') return decision
    const modePath = join(bridgeDir, `${sessionId}.mode.json`)
    const wanted = await readJson(modePath)
    if (wanted === null) return decision
    await removeQuietly(modePath)
    try {
      const outcome = ctx.planMode.set(agent, wanted.plan === true)
      logger?.info?.(`plan mode set -> ${wanted.plan === true ? 'on' : 'off'} (${outcome})`)
    } catch (error) {
      logger?.warn?.(`plan mode set failed: ${error}`)
    }
    return decision
  })

  ctx.on('user-questions/request', async (request, next) => {
    const delegate =
      typeof next === 'function' ? next : () => Promise.reject(new Error('no downstream answerer'))
    const question = planQuestionOf(request)
    if (question === undefined) return await delegate()

    const reviewId = reviewIdOf(request)
    if (reviewId === '') return await delegate()

    const reviewPath = join(bridgeDir, `${reviewId}.review.json`)
    const answerPath = join(bridgeDir, `${reviewId}.answer.json`)
    const approveLabel = String(question?.intent?.approve ?? 'Approve')

    // 先把计划落到桥接目录（后端即便错过 SSE 事件也能读到），同时清掉上一轮旧结论。
    await removeQuietly(answerPath)
    await writeFile(
      reviewPath,
      JSON.stringify({
        review_id: reviewId,
        header: question.header ?? 'Plan review',
        question: question.question ?? 'Approve this plan and leave plan mode?',
        approve: approveLabel,
        options: question.options ?? [],
        plan: question.detail,
        created_at: Date.now(),
      }),
      'utf8',
    )
    logger?.info?.(`plan review waiting: ${reviewId}`)

    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      const answer = await readJson(answerPath)
      if (answer !== null) {
        await removeQuietly(answerPath)
        await removeQuietly(reviewPath)
        if (answer.approved === true) {
          logger?.info?.(`plan review approved: ${reviewId}`)
          return { answers: [{ id: question.id, selected: [approveLabel] }] }
        }
        const feedback = typeof answer.feedback === 'string' ? answer.feedback.trim() : ''
        logger?.info?.(`plan review kept planning: ${reviewId}`)
        // 未批准：以「保持规划 + 用户反馈」作答，模型据此修订并再次呈交。
        return { answers: [{ id: question.id, selected: [], custom: feedback }] }
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
    }

    await removeQuietly(reviewPath)
    logger?.warn?.(`plan review timed out: ${reviewId}`)
    // 官方契约：评审通道不可用/中断时必须留在计划模式，不能静默继续实现。
    const error = new Error(
      'The user did not answer the plan review in time; stay in plan mode and stop here.',
    )
    error.name = 'UserQuestionError'
    error.code = 'ASK_CANCELLED'
    throw error
  })
}
