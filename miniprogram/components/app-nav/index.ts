Component({
  properties: { title: { type: String, value: 'cola知识库' }, context: { type: String, value: '' }, showContext: { type: Boolean, value: true }, menu: { type: Boolean, value: false }, back: { type: Boolean, value: false }, conversation: { type: Boolean, value: false }, action: { type: String, value: '' }, discover: { type: Boolean, value: false } },
  data: { navStyle: 'height:88px;', barStyle: 'height:88px;padding-top:44px;', innerStyle: 'height:44px;', discoverStyle: 'right:92px;' },
  lifetimes: { attached() { this.measure() } },
  pageLifetimes: { show() { this.measure() }, resize() { this.measure() } },
  methods: {
    measure() {
      const api = wx as any
      const info = api.getWindowInfo ? api.getWindowInfo() : wx.getSystemInfoSync()
      const rect = wx.getMenuButtonBoundingClientRect()
      const status = info.statusBarHeight || 20
      const valid = rect && rect.height > 0 && rect.top >= status
      const height = valid ? Math.max(44, (rect.top - status) * 2 + rect.height) : 44
      const reserve = Math.max(60, valid ? info.windowWidth - rect.left + 12 : 96)
      const discoverRight = Math.max(12, valid ? info.windowWidth - rect.left + 8 : 92)
      this.setData({ navStyle: `height:${status + height}px;`, barStyle: `height:${status + height}px;padding-top:${status}px;`,
        innerStyle: `height:${height}px;padding-right:${reserve}px;`, discoverStyle: `right:${discoverRight}px;` })
    },
    goBack() {
      if (this.data.conversation) { this.triggerEvent('conversationback'); return }
      if (getCurrentPages().length > 1) wx.navigateBack(); else wx.reLaunch({ url: '/pages/chat/index' })
    },
    openProfile() { const pages = getCurrentPages(); const current = pages[pages.length - 1] as any; if (current?.route === 'pages/mine/index') return; wx.navigateTo({ url: '/pages/mine/index' }) },
    openContext() { this.triggerEvent('contexttap') },
    openMenu() { this.triggerEvent('menutap') },
    openAction() { this.triggerEvent('actiontap') },
    openDiscover() { wx.navigateTo({ url: '/pages/market/index' }) },
  },
})
