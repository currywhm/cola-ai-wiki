// 「分享给微信好友」的转发内容构造。
// 三个会话页（问AI / 知识库 / 文件夹）共用这一份实现，保证按钮行为与卡片内容完全一致。
//
// 微信要求 onShareAppMessage 同步返回转发内容，而分享卡片要先在服务端落库才能拿到 id。
// 这里用官方支持的 promise 字段（基础库 2.12.0 起）：先同步返回标题与兜底路径让面板秒开，
// 同一次转发再把落库结果通过 promise 补上真实路径；3 秒内没落库完成就退回兜底路径
// （打开小程序首页），不会出现「点了没反应」的情况。
import { createShare, ShareSourcePayload } from '../services/api'
import { collectSelection } from './pick'

const MAX_ANSWER = 12000
const MAX_QUESTION = 300
const MAX_SOURCES = 12

// 转发卡片封面：5:4（500x400），品牌头像居中，微信转发卡片按 5:4 裁切也不丢内容
export const SHARE_IMAGE = '/assets/brand/cola-share.jpg'
export const SHARE_HOME_PATH = '/pages/ask/index'

export type SharePayload = {
  title: string
  path: string
  imageUrl: string
  promise?: Promise<{ title: string; path: string; imageUrl: string }>
}

// 同一条回答重复转发时复用已落库的卡片，不重复建记录
const cardCache: Record<string, string> = {}

function fingerprint(question: string, answer: string): string {
  const text = `${question}\u0000${answer}`
  let hash = 5381
  for (let index = 0; index < text.length; index += 1) hash = ((hash * 33) ^ text.charCodeAt(index)) >>> 0
  return `${hash.toString(36)}:${text.length}`
}

// 转发标题：优先用用户自己的提问（好友一眼知道问了什么），没有提问就用回答首句
export function shareTitle(question: string, answer: string): string {
  const asked = oneLine(question)
  if (asked) return asked.length > 36 ? `${asked.slice(0, 35)}…` : asked
  const opening = oneLine(answer)
  if (!opening) return 'cola知识库 · 用 AI 读懂你的资料'
  return opening.length > 36 ? `${opening.slice(0, 35)}…` : opening
}

// 去掉 Markdown 记号与换行，只留一行能当标题的纯文本
function oneLine(text: string): string {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[#>*`~|[\]()]/g, ' ')
    .replace(/!?\S*\.(png|jpe?g|gif|webp)\b/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function shareSources(sources: any[] | undefined): ShareSourcePayload[] {
  const list = Array.isArray(sources) ? sources : []
  const seen: Record<string, boolean> = {}
  const result: ShareSourcePayload[] = []
  for (const source of list) {
    const filename = String((source && (source.filename || source.name)) || '').slice(0, 200)
    const url = String((source && source.url) || '').slice(0, 2048)
    if (!filename && !url) continue
    const key = `${filename}|${url}`
    if (seen[key]) continue
    seen[key] = true
    result.push({ filename, page_number: Number((source && source.page_number) || 0) || 0, url })
    if (result.length >= MAX_SOURCES) break
  }
  return result
}

export function homePayload(): SharePayload {
  return { title: 'cola知识库 · 用 AI 读懂你的资料', path: SHARE_HOME_PATH, imageUrl: SHARE_IMAGE }
}

/** 为一条回答生成转发内容：先给兜底卡片，落库完成后补上真实分享路径。 */
export function buildSharePayload(input: { message?: any; question?: string; knowledgeName?: string }): SharePayload {
  const answer = String((input.message && input.message.content) || '').trim()
  if (!answer) return homePayload()
  const question = String(input.question || '').slice(0, MAX_QUESTION)
  const knowledgeName = String(input.knowledgeName || '').slice(0, 80)
  const title = shareTitle(question, answer)
  const card = { title, path: SHARE_HOME_PATH, imageUrl: SHARE_IMAGE }

  const cached = cardCache[fingerprint(question, answer)]
  if (cached) return { ...card, path: `/package-features/pages/share/index?id=${cached}` }

  const promise = createShare({
    question,
    answer: answer.slice(0, MAX_ANSWER),
    knowledge_name: knowledgeName,
    sources: shareSources(input.message && input.message.sources),
  }).then((result) => {
    const id = String((result && result.id) || '')
    if (!id) return card
    cardCache[fingerprint(question, answer)] = id
    return { ...card, path: `/package-features/pages/share/index?id=${id}` }
  }).catch(() => card)

  return { ...card, promise }
}

/** 在消息列表里找这条回答对应的提问：往回找最近的一条用户消息。 */
const MAX_FILES = 8

/**
 * 为「多选分享」生成转发内容：勾中的内容与文件一起落成一张分享卡片。
 * 与单条分享共用同一套缓存与兑底：先同步给一张卡片让面板秒开，落库完成后再补真实路径。
 */
export function buildSelectionPayload(input: { messages: any[]; keys: string[]; knowledgeName?: string }): SharePayload {
  const selection = collectSelection(input.messages || [], input.keys || [], input.knowledgeName || '')
  if (!selection.count) return homePayload()
  const answer = selection.text || (selection.fileCount ? `分享了 ${selection.fileCount} 个文件` : '')
  if (!answer) return homePayload()
  const title = (selection.title || 'cola知识库').slice(0, 40)
  const card = { title, path: SHARE_HOME_PATH, imageUrl: SHARE_IMAGE }
  const cacheKey = `pick:${fingerprint(title, answer)}`
  const cached = cardCache[cacheKey]
  if (cached) return { ...card, path: `/package-features/pages/share/index?id=${cached}` }

  const files: { artifact_id: string; name: string }[] = []
  const attached = selection.files || []
  for (let index = 0; index < attached.length && index < MAX_FILES; index += 1) {
    files.push({ artifact_id: String(attached[index].id || ''), name: String(attached[index].name || '') })
  }

  const promise = createShare({
    title,
    question: '' ,
    answer: answer.slice(0, MAX_ANSWER),
    knowledge_name: String(input.knowledgeName || '').slice(0, 80),
    sources: shareSources(selection.sources),
    files,
  }).then((result) => {
    const id = String((result && result.id) || '')
    if (!id) return card
    cardCache[cacheKey] = id
    return { ...card, path: `/package-features/pages/share/index?id=${id}` }
  }).catch(() => card)

  return { ...card, promise }
}
export function questionFor(messages: any[], index: number): string {
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const message = messages[cursor]
    if (message && message.role === 'user') return String(message.content || '')
  }
  return ''
}
