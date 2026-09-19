/// <reference path="./types/index.d.ts" />

interface IAppOption {
  globalData: {
    userInfo?: WechatMiniprogram.UserInfo,
    token: string,
    user?: { id?: string, nickname?: string, avatar?: string } | null,
    loggedOut?: boolean,
    // 用户是否已同意《小程序用户隐私保护指引》。拒绝也不影响登录问答，只是相册/拍照/文件选择在同意前不可用。
    privacyGranted?: boolean,
    pendingImportFile?: { path: string, filename: string } | null,
    // 启动页只在小程序冷启动时出现一次，切 tab 不再重复播放
    splashShown?: boolean,
  }
  captureOpenFile?: (options?: any) => void,
  userInfoReadyCallback?: WechatMiniprogram.GetUserInfoSuccessCallback,
}
