type ChatTarget = { knowledgeId: string; conversationId?: string }
let pendingChat: ChatTarget | null = null
export function openChat(target: ChatTarget) {
  pendingChat = target
  wx.navigateTo({ url: '/pages/chat/index' })
}
export function consumeChatTarget(): ChatTarget | null {
  const target = pendingChat
  pendingChat = null
  return target
}
