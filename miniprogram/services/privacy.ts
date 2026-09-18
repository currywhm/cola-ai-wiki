// 小程序隐私保护指引授权：统一在这里判断，避免各页面自己分叉。
//
// 依据微信《小程序隐私协议开发指南》（framework/user-privacy/PrivacyAuthorize.html）：
//   平台自带「官方隐私授权弹窗」：只要调用了隐私相关接口，微信自己判断这次调用要不要授权，
//   要就弹出官方弹窗（标题「用户隐私保护提示」，正文就是《小程序用户隐私保护指引》全文，
//   底部「拒绝 / 同意」两个按钮）。官方原话：此弹窗「无需开发者适配开发，自动向 C 端用户展示」。
//
// 启动时不弹：不该在用户还没做任何操作时就打断他；真正要授权时才弹，用户更能接受。
// 我们走微信「官方隐私授权弹窗」这条路线，刻意什么都不接管：
//   * 不注册 wx.onNeedPrivacyAuthorization：一旦注册，就必须自己弹窗并回传 resolve，
//     官方弹窗反而不会出现（之前用户看到的始终是自绘弹层，就是这个原因）；
//   * 不写自绘授权弹层：官方弹窗的样式、措辞和合规记录都由平台保证。
// 只有少数场景（用户主动点了入口、我们想先把话说清楚）才显式调 wx.requirePrivacyAuthorize()。
// 注意：<input type="nickname"> 在未同意时会静默降级成普通输入框，所以「我要填昵称」必须先弹指引。
//
// 当前小程序真正会触发的隐私接口只有三类，对应后台要申报的项：
//   收集你的昵称、头像          <button open-type="chooseAvatar"> / <input type="nickname">
//   收集你选中的照片或视频信息  wx.chooseMedia / wx.chooseImage  （相册导入、拍照扫描）
//   收集你选中的文件          wx.chooseMessageFile            （微信文件导入）

export type PrivacySetting = { needAuthorization: boolean; privacyContractName?: string }

function api() { return wx as any }

export function privacySupported(): boolean {
  return typeof api().getPrivacySetting === 'function'
}

// 查询当前是否还需要用户授权。老基础库或不支持的端直接当作已授权，不能卡住主流程。
export function readPrivacySetting(): Promise<PrivacySetting> {
  if (!privacySupported()) return Promise.resolve({ needAuthorization: false })
  return new Promise((resolve) => {
    api().getPrivacySetting({
      success: (result: any) => resolve({
        needAuthorization: !!(result && result.needAuthorization),
        privacyContractName: (result && result.privacyContractName) || '',
      }),
      fail: () => resolve({ needAuthorization: false }),
    })
  })
}

// 触发官方隐私授权弹窗（弹窗内展示《小程序用户隐私保护指引》内容），
// 返回 true 表示用户点了同意，false 表示拒绝。
export function requestPrivacyAuthorize(): Promise<boolean> {
  if (typeof api().requirePrivacyAuthorize !== 'function') return Promise.resolve(true)
  return new Promise((resolve) => {
    api().requirePrivacyAuthorize({
      success: () => resolve(true),
      fail: () => resolve(false),
    })
  })
}

// 已经同意就直接返回 true；还需要授权就弹官方弹窗等用户选。
export function ensurePrivacyAuthorized(): Promise<boolean> {
  return readPrivacySetting()
    .then((setting) => (setting.needAuthorization ? requestPrivacyAuthorize() : true))
    .catch(() => true)
}

// 用户拒绝时的统一提示：只用一句 toast。
// 这里刻意不再用 wx.showModal 自绘弹窗——用户要看的是微信官方那个「用户隐私保护提示」，
// 自绘弹窗长得像官方弹窗只会让人分不清哪个才算数（之前就是这么被指出的）。
// 另外官方规则：距上次拒绝不足 10 秒不会再弹，所以这里也不做「自动再弹一次」。
export function warnPrivacyRequired(action: string) {
  wx.showToast({ title: `同意《小程序用户隐私保护指引》后才能${action}`, icon: 'none', duration: 2600 })
}

// 打开微信官方的《小程序用户隐私保护指引》页面（名称与内容都来自小程序后台的配置）。
// 官方弹窗里写的正是这份指引，所以「查看全文」也打开同一份，读到的和同意的是同一个东西。
// 低版本基础库没有这个接口，返回 false 交给调用方兜底（跳我们自己的页面）。
export function openPrivacyContract(): boolean {
  if (typeof api().openPrivacyContract !== 'function') return false
  try {
    api().openPrivacyContract({ fail: () => { /* 打不开就算了，调用方已有兜底入口 */ } })
    return true
  } catch (error) {
    return false
  }
}
