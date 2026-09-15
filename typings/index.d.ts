/// <reference path="./types/index.d.ts" />

interface IAppOption {
  globalData: {
    userInfo?: WechatMiniprogram.UserInfo,
    apiBase: string,
    token: string,
    user?: { id?: string, nickname?: string, avatar?: string } | null,
    loggedOut?: boolean,
    pendingImportFile?: { path: string, filename: string } | null,
  }
  captureOpenFile?: (options?: any) => void,
  userInfoReadyCallback?: WechatMiniprogram.GetUserInfoSuccessCallback,
}
