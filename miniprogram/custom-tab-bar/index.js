Component({
  options: { styleIsolation: 'apply-shared' },
  data: { selected: 0, hidden: false, safeBottom: 0 },
  lifetimes: {
    attached() {
      let bottom = 0
      try {
        const info = wx.getWindowInfo ? wx.getWindowInfo() : wx.getSystemInfoSync()
        const area = info && info.safeArea
        if (area) bottom = Math.max(0, Math.round((info.windowHeight || 0) - (area.bottom || 0)))
      } catch (e) { bottom = 0 }
      if (bottom !== this.data.safeBottom) this.setData({ safeBottom: bottom })
    }
  },
  methods: {
    switchTab(e) {
      const index = Number(e.currentTarget.dataset.index)
      const path = e.currentTarget.dataset.path
      if (!path || index === this.data.selected) return
      const previous = this.data.selected
      this.setData({ selected: index })
      wx.switchTab({ url: path, fail: () => this.setData({ selected: previous }) })
    }
  }
})
