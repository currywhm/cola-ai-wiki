/// <reference path="./types/index.d.ts" />

interface IAppOption {
  globalData: {
    userInfo?: WechatMiniprogram.UserInfo,
    apiBase: string,
    token: string,
    user?: { id?: string, nickname?: string, avatar?: string } | null,
    loggedOut?: boolean,
    pendingImportFile?: { path: string, filename: string } | null,
    // 启动页只在小程序冷启动时出现一次，切 tab 不再重复播放
    splashShown?: boolean,
  }
  captureOpenFile?: (options?: any) => void,
  userInfoReadyCallback?: WechatMiniprogram.GetUserInfoSuccessCallback,
}
