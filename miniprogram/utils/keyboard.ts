import { pinTail } from './thread'

/**
 * 会话页键盘避让：问AI / 文件夹会话 / 知识库会话三处共用同一套算法。
 *
 * 三个页面都是写死高度的固定布局（height:100vh + overflow:hidden + 内部 flex 滚动）。
 * 微信的 adjust-position 靠上推整个网页来避让键盘，固定布局推不动，键盘就直接盖住
 * 输入框。所以统一关掉自动上推，改成按键盘高度自己收缩容器：
 *   height: calc(100vh - 键盘高度)
 * 键盘弹起时同时清掉底部安全区留白——那块空间已经被键盘占住，留着会让输入框悬空一截。
 */

/** 键盘高度（px，取整）；键盘收起时为 0。 */
export function readKeyboardHeight(e: any): number {
  const height = Number((e && e.detail && e.detail.height) || 0)
  return height > 0 ? Math.round(height) : 0
}

/**
 * 页面根容器的内联样式。
 *
 * @param safeBottom 实测的 Home 条高度（px）
 * @param keyboardHeight 键盘高度（px），0 表示收起
 * @param paddingBottom 键盘收起时的 padding-bottom 表达式；不传则用 safeBottom
 */
export function shellStyle(safeBottom: number, keyboardHeight: number, paddingBottom = ''): string {
  const height = `height:calc(100vh - ${keyboardHeight}px);`
  if (keyboardHeight > 0) return `${height}padding-bottom:0;`
  const pad = paddingBottom || (safeBottom > 0 ? `${safeBottom}px` : '')
  return pad ? `${height}padding-bottom:${pad};` : height
}

/** 弹层（选择知识库 / 模型 / 技能 / 历史）打开前收起键盘：
    输入框还带着焦点时，键盘会压在弹层上，选完还得手动收一次。 */
export function dismissKeyboard(): void {
  try {
    wx.hideKeyboard({ fail: () => undefined })
  } catch (e) { /* 个别环境没有这个 API，忽略 */ }
}

/** 容器变矮之后，把消息列表重新贴回最新一条（跟 thread 的滚动跟随共用一套）。 */
export function repinLatest(page: any): void {
  pinTail(page)
}
