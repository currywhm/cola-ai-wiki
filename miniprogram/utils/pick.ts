/**
 * 对话结束后的「多选 → 分享」：选中态的键、选中内容的收集、以及分享文案。
 *
 * 三个会话页（问AI / 知识库 / 文件夹）共用同一份实现，保证勾选规则与分享内容完全一致：
 *  · 消息以 `msg:<消息 id>` 记账，文件以 `file:<产物 id>` 记账，两者可以分开勾；
 *  · 开场白 / 本地提示 / 正在生成的回答不可勾选，避免把「思考中」分享出去；
 *  · 分享文案按 问 / 答 标注，勾了多条时依然读得懂是谁问的、答的是什么。
 */

export const PICK_MSG_PREFIX = 'msg:'
export const PICK_FILE_PREFIX = 'file:'

export type PickSelection = {
  count: number
  messageCount: number
  fileCount: number
  title: string
  text: string
  files: any[]
  artifactIds: string[]
  sources: any[]
}

export function messageKey(id: string): string {
  return PICK_MSG_PREFIX + String(id || '')
}

export function fileKey(id: string): string {
  return PICK_FILE_PREFIX + String(id || '')
}

export function isPicked(keys: string[] | undefined, key: string): boolean {
  return (keys || []).indexOf(key) >= 0
}

/** 一条消息能不能被勾选：正文 + 非开场白 + 非本地提示。 */
export function canPickMessage(message: any): boolean {
  if (!message) return false
  if (message.greeting || message.local) return false
  return !!String(message.content || '').trim()
}

/** 当前会话里所有可勾选的键（用于「全选」）。 */
export function pickableKeys(messages: any[]): string[] {
  const keys: string[] = []
  const list = messages || []
  for (let index = 0; index < list.length; index += 1) {
    const message = list[index]
    if (canPickMessage(message)) keys.push(messageKey(message.id))
    const files = (message && message.artifacts) || []
    for (let cursor = 0; cursor < files.length; cursor += 1) keys.push(fileKey(files[cursor].id))
  }
  return keys
}

function oneLine(text: string, limit: number): string {
  const flat = String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[#>*`~|[\]()]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  if (flat.length <= limit) return flat
  return flat.slice(0, limit - 1) + '…'
}

/** 把勾选的消息与文件收敛成一次分享所需的东西。 */
export function collectSelection(messages: any[], keys: string[], knowledgeName: string): PickSelection {
  const picked = keys || []
  const list = messages || []
  const blocks: string[] = []
  const files: any[] = []
  const sources: any[] = []
  let messageCount = 0

  for (let index = 0; index < list.length; index += 1) {
    const message = list[index]
    if (!message) continue
    if (canPickMessage(message) && isPicked(picked, messageKey(message.id))) {
      messageCount += 1
      const label = message.role === 'user' ? '问' : '答'
      blocks.push(`【${label}】\n${String(message.content || '').trim()}`)
      const list2 = message.sources || []
      for (let cursor = 0; cursor < list2.length; cursor += 1) sources.push(list2[cursor])
    }
    const attached = message.artifacts || []
    for (let cursor = 0; cursor < attached.length; cursor += 1) {
      const file = attached[cursor]
      if (file && isPicked(picked, fileKey(file.id))) files.push(file)
    }
  }

  const body = blocks.join('\n\n')
  let title = ''
  if (messageCount === 1 && files.length === 0) {
    title = oneLine(blocks[0].replace(/^【[问答]】\s*/, ''), 36)
  } else if (messageCount === 0 && files.length) {
    title = files.length === 1 ? `分享了文件 · ${files[0].name}` : `分享了 ${files.length} 个文件`
  } else {
    title = `${knowledgeName || 'cola知识库'} · ${messageCount + files.length} 项内容`
  }

  const artifactIds: string[] = []
  for (let index = 0; index < files.length; index += 1) artifactIds.push(String(files[index].id || ''))

  return {
    count: messageCount + files.length,
    messageCount,
    fileCount: files.length,
    title: title || 'cola知识库',
    text: body,
    files,
    artifactIds,
    sources,
  }
}

/** 分享面板底部那行说明：把「点下去会发生什么」提前讲清楚。 */
export function shareHint(target: string, selection: PickSelection): string {
  const hasText = selection.messageCount > 0
  const fileCount = selection.fileCount
  if (target === 'wechat') {
    if (!hasText && fileCount) return fileCount === 1 ? '文件会直接发给微信好友' : `${fileCount} 个文件会随卡片一起发给好友`
    return fileCount ? '内容做成卡片发给好友，文件会一起带过去' : '内容做成卡片发给好友，点开即可查看'
  }
  if (target === 'qq' || target === 'dingtalk') {
    const name = target === 'qq' ? 'QQ' : '钉钉'
    if (!hasText) return `只勾了文件，${name} 收不到文件，改用微信或存到知识库`
    return `微信不支持直接跳转 ${name}：内容会复制到剪贴板，粘贴即可发送`
  }
  if (target === 'knowledge') return '选一个知识库，把勾选的内容与文件存进去'
  return ''
}
