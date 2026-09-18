import { AGREEMENT_VERSION, markAgreementsAccepted, resumeWechatLogin } from '../../services/api'

Page({
  data: {
    agreementVisible: false,
    agreementChecked: false,
    loggingIn: false,
  },
  onLoad() {
    // 已有登录态时不再让用户重复登录；无登录态则停留本页，未勾选协议时点击登录才展示提示。
    if (wx.getStorageSync('llmwiki_token')) {
      wx.reLaunch({ url: '/pages/ask/index' })
    }
  },
  back() {
    wx.switchTab({ url: '/pages/ask/index' })
  },
  openDoc(e: any) {
    const type = String(e.currentTarget.dataset.type || 'terms')
    wx.navigateTo({ url: `/package-features/pages/legal/index?type=${type}` })
  },
  login() {
    if (this.data.loggingIn) return
    if (!this.data.agreementChecked) {
      this.setData({ agreementVisible: true })
      return
    }
    this.performLogin()
  },
  toggleAgreement() {
    this.setData({ agreementChecked: !this.data.agreementChecked })
  },
  closeAgreement() {
    if (this.data.loggingIn) return
    this.setData({ agreementVisible: false })
  },
  rejectAgreement() {
    this.closeAgreement()
  },
  stopTap() { return },
  agreeAndLogin() {
    if (this.data.loggingIn) return
    this.setData({ agreementChecked: true, agreementVisible: false })
    this.performLogin()
  },
  performLogin() {
    if (this.data.loggingIn) return
    this.setData({ loggingIn: true })
    markAgreementsAccepted(AGREEMENT_VERSION)
    resumeWechatLogin()
      .then(() => {
        wx.reLaunch({ url: '/pages/ask/index' })
      })
      .catch((error: any) => {
        wx.showToast({ title: error?.message || '微信登录失败，请稍后重试', icon: 'none' })
      })
      .finally(() => this.setData({ loggingIn: false }))
  },
})
