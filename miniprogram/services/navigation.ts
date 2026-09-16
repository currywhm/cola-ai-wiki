// folderId 用于「最近」里点文件夹：跳到知识库页并直接落在该文件夹的目录视图
// （不进会话，也不进文档页——文件夹不是文档）。
type ChatTarget = { knowledgeId: string; conversationId?: string; folderId?: string }
let pendingChat: ChatTarget | null = null
export function openChat(target: ChatTarget) {
  pendingChat = target
  wx.switchTab({ url: '/pages/chat/index' })
}
export function consumeChatTarget(): ChatTarget | null {
  const target = pendingChat
  pendingChat = null
  return target
}
