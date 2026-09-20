/**
 * 统一复制入口。
 *
 * 复制失败最容易被当成代码问题，其实是平台侧没声明：小程序把剪贴板列为隐私接口，
 * 没在「微信公众平台 → 设置 → 基本设置 → 用户隐私保护指引」里声明「剪贴板」时，
 * wx.setClipboardData 会直接 fail，errMsg 是
 * 「setClipboardData:fail api scope is not declared in the privacy agreement」。
 * 这里把原始 errMsg 打到控制台（开发者工具 Console 可见），界面只给一句人话。
 */
export function copyText(text: string, tip = '已复制', onSuccess?: () => void): void {
  const data = String(text || '')
  if (!data) return
  wx.setClipboardData({
    data,
    success: () => {
      if (onSuccess) onSuccess()
      wx.showToast({ title: tip, icon: 'success' })
    },
    fail: (res: any) => {
      console.error('[clipboard] 复制失败：', (res && res.errMsg) || res)
      wx.showToast({ title: '复制失败，请稍后重试', icon: 'none' })
    },
  })
}
