// 图标名 → 素材文件的唯一映射。ui-icon 组件和「直接用 <image> 画图标」的页面共用这一份，
// 避免同一个名字在两处对不上。
// 为什么需要直接画：技能编辑页的图标格子一次要出 14 个图标，走自定义组件每个都要 attached、
// 观察器和 setData，进页首屏会被拖慢一两百毫秒；这里直接给文件路径，页面用 <image> 画，零组件开销。
export const ICON_ROOT = '/assets/icons/cola-set/'

export type IconConfig = { file: string; transform?: string }

export const ICONS: Record<string, IconConfig> = {
  // Direct matches from the supplied icon collection.
  back: { file: 'fanhui.svg' },
  fanhui: { file: 'fanhui.svg' },
  'chevron-left': { file: 'fanhui.svg' },
  search: { file: 'sousuo.svg' },
  sousuo: { file: 'sousuo.svg' },
  plus: { file: 'tianjia.svg' },
  tianjia: { file: 'tianjia.svg' },
  menu: { file: 'liebiao.svg' },
  liebiao: { file: 'liebiao.svg' },
  more: { file: 'liebiao.svg' },
  compass: { file: 'discover-custom.svg' },
  faxian: { file: 'discover-custom.svg' },
  web: { file: 'scope-web.svg' },
  wangluo: { file: 'wangluo.svg' },
  shield: { file: 'anquanbaozhang.svg' },
  anquanbaozhang: { file: 'anquanbaozhang.svg' },
  data: { file: 'shuju.svg' },
  shuju: { file: 'shuju.svg' },
  info: { file: 'dengpao.svg' },
  dengpao: { file: 'dengpao.svg' },
  agreement: { file: 'jieshao.svg' },
  jieshao: { file: 'jieshao.svg' },
  user: { file: 'guanyu.svg' },
  guanyu: { file: 'guanyu.svg' },
  // 「我的」页入口：使用与登录态切换前保持同一套专属图标
  'mine-login': { file: 'mine-login.svg' },
  'mine-usage': { file: 'mine-usage.svg' },
  'mine-membership': { file: 'mine-membership.svg' },
  'mine-tips': { file: 'mine-tips.svg' },
  'mine-service': { file: 'mine-service.svg' },
  'mine-logout': { file: 'mine-logout.svg' },
  folder: { file: 'folder.svg' },
  'folder-green': { file: 'folder-green.svg' },
  'folder-add': { file: 'folder-add-custom.svg' },
  file: { file: 'upload-doc.svg' },
  knowledge: { file: 'scope-knowledge.svg' },
  response: { file: 'dui.svg' },
  dui: { file: 'dui.svg' },
  check: { file: 'dui.svg' },
  'agreement-unchecked': { file: 'agreement-unchecked.svg' },
  'agreement-checked': { file: 'agreement-checked.svg' },
  spark: { file: 'dengpao.svg' },
  atom: { file: 'atom.svg' },
  vip: { file: 'vip.svg' },
  'knowledge-mode': { file: 'scope-knowledge.svg' },
  copy: { file: 'copy.svg' },
  share: { file: 'share.svg' },
  // 对话产物卡右侧的「保存」：下到本地后转发到微信聊天（可继续保存到手机）
  download: { file: 'download.svg' },
  save: { file: 'download.svg' },
  pin: { file: 'pin.svg' },
  'pin-white': { file: 'pin-white.svg' },
  home: { file: 'guanyu.svg' },
  help: { file: 'guanyu.svg' },
  settings: { file: 'liebiao.svg' },
  trash: { file: 'trash.svg' },
  // 技能面板：收藏 / 点赞 / 我的技能（选中态用实心图标，与底部导航一致）
  shoucang: { file: 'shoucang.svg' },
  'shoucang-on': { file: 'shoucang-on.svg' },
  dianzan: { file: 'dianzan.svg' },
  'dianzan-on': { file: 'dianzan-on.svg' },
  geren: { file: 'geren.svg' },
  tag: { file: 'tag.svg' },
  // 联系客服：微信原生客服会话入口，图标是自绘的耳机，与整套线条风格一致
  kefu: { file: 'kefu.svg' },
  support: { file: 'kefu.svg' },
  upload: { file: 'document-add.svg' },
  'wechat-file': { file: 'wechat-custom.svg' },
  wechat: { file: 'wechat-custom.svg' },
  'wechat-friend': { file: 'wechat-custom.svg' },
  'wechat-white': { file: 'wechat-white.svg' },
  // 分享面板：QQ 与钉钉（自绘的抽象图形，不用第三方商标）
  qq: { file: 'qq.svg' },
  dingtalk: { file: 'dingtalk.svg' },
  logout: { file: 'logout.svg' },
  gongzhonghao: { file: 'gongzhonghao.svg' },
  'wechat-article': { file: 'gongzhonghao.svg' },
  image: { file: 'album-custom.svg' },
  'image-blue': { file: 'image-blue.svg' },
  camera: { file: 'camera-custom.svg' },
  filter: { file: 'liebiao.svg' },
  chat: { file: 'guanyu.svg' },
  send: { file: 'send-ready.svg' },
  'send-white': { file: 'send-ready.svg' },
  // 输入框发送键两态：可发送=蓝色实心，待输入=灰色实心（与停止键同一套尺寸）
  'send-ready': { file: 'send-ready.svg' },
  'send-idle': { file: 'send-idle.svg' },
  stop: { file: 'stop.svg' },
  tingzhi: { file: 'stop.svg' },
  history: { file: 'history.svg' },
  'ask-hero': { file: 'ask-hero.svg' },
  refresh: { file: 'refresh.svg' },
  book: { file: 'book.svg' },
  ppt: { file: 'ppt.svg' },
  arrow: { file: 'fanhui.svg', transform: 'transform:scaleX(-1);' },
  'chevron-right': { file: 'fanhui.svg', transform: 'transform:scaleX(-1);' },
  'chevron-down': { file: 'chevron-down.svg' },
  'chevron-up': { file: 'chevron-down.svg', transform: 'transform:rotate(180deg);' },
  close: { file: 'tianjia.svg', transform: 'transform:rotate(45deg);' },
  // 最近列表使用的文件类型标识（自绘，不含第三方商标）
  'recent-pdf': { file: 'recent-pdf.svg' },
  'recent-folder': { file: 'recent-folder.svg' },
  'recent-doc': { file: 'recent-doc.svg' },
  'recent-ppt': { file: 'recent-ppt.svg' },
  'recent-sheet': { file: 'recent-sheet.svg' },
  'recent-image': { file: 'recent-image.svg' },
  'recent-text': { file: 'recent-text.svg' },
  robot: { file: 'robot.svg' },
  'robot-blue': { file: 'robot-blue.svg' },
  // 输入框工具条：小机器人两态（执行规划 / 基于知识库问答）、技能、选择知识库、模型选项
  'robot-planner': { file: 'robot-planner.svg' },
  'robot-knowledge': { file: 'robot-knowledge.svg' },
  skill: { file: 'skill-node.svg' },
  'skill-node': { file: 'skill-node.svg' },
  // 选中态：线条变蓝，作为「本轮已启用技能」的唯一提示
  'skill-blue': { file: 'skill-node-blue.svg' },
  'skill-node-blue': { file: 'skill-node-blue.svg' },
  'knowledge-pick': { file: 'knowledge-pick.svg' },
  'model-depth': { file: 'model-depth.svg' },
  'agent-mode': { file: 'agent-mode.svg' },
  planner: { file: 'agent-mode.svg' },
  agent: { file: 'robot.svg' },
  sliders: { file: 'sliders.svg' },
  tune: { file: 'sliders.svg' },
  // 增强提示词：四角星（AI 润色 / 生成语义），技能编辑页指令框右下角用它
  enhance: { file: 'enhance.svg' },
  star: { file: 'enhance.svg' },
  sijiaoxing: { file: 'enhance.svg' },
  at: { file: 'at.svg' },
  mention: { file: 'at.svg' },
}

export function resolveIcon(name: string): IconConfig {
  return ICONS[name] || ICONS.file
}

// 给 <image src=""> 用的完整路径：找不到的名字回落到通用文件图标，和 ui-icon 的行为保持一致
export function iconPath(name: string): string {
  return ICON_ROOT + resolveIcon(name).file
}
