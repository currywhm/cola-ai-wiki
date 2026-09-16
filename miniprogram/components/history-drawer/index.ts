// 历史对话抽屉：会话页左上角「折叠」图标拉出的左抽屉，用来切换 / 置顶 / 删除历史会话。
// 数据全部来自后端 /api/conversations（按用户 + 知识库 + 文件夹收窄），组件本身不缓存任何内容。

interface ConversationItem {
  id: string
  title?: string
  updated_at?: string
  knowledge_id?: string
  folder_id?: string
  pinned?: number | boolean
}

// 手指横向移动超过这个距离才判定为「滑开 / 收回」，避免误触
const SWIPE_THRESHOLD = 24

function startOfToday(): number {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
}

Component({
  properties: {
    visible: { type: Boolean, value: false },
    loading: { type: Boolean, value: false },
    activeId: { type: String, value: '' },
    items: { type: Array, value: [] },
  },
  data: {
    statusTop: 20,
    query: '',
    searching: false,
    groups: [] as any[],
    // 当前被左滑打开的那一行；同一时刻只允许一行打开
    openId: '',
    startX: 0,
    startY: 0,
  },
  lifetimes: {
    attached() {
      // 抽屉是通栏的，必须自己避让状态栏，否则标题会压到刘海/时间上
      try {
        const info = (wx as any).getWindowInfo ? (wx as any).getWindowInfo() : wx.getSystemInfoSync()
        this.setData({ statusTop: Math.max(20, Number(info.statusBarHeight) || 20) })
      } catch (error) { /* 取不到就走默认值 */ }
    },
  },
  observers: {
    items() { this.buildGroups() },
    query() { this.buildGroups() },
    visible(value: boolean) {
      if (value) {
        this.setData({ searching: false, query: '', openId: '' })
        this.buildGroups()
      }
    },
  },
  methods: {
    // 置顶的会话单独成组排在最前，其余按「今天 / 近 7 天 / 更早」分组；搜索词只过滤标题
    buildGroups() {
      const keyword = String(this.data.query || '').trim().toLowerCase()
      const source = ((this.data.items || []) as ConversationItem[]).filter((item) => !keyword || String(item.title || '').toLowerCase().includes(keyword))
      const today = startOfToday()
      const week = today - 6 * 86400000
      const pinned: any[] = []
      const buckets = [
        { key: 'today', label: '今天', items: [] as any[] },
        { key: 'week', label: '近7天', items: [] as any[] },
        { key: 'older', label: '更早', items: [] as any[] },
      ]
      source.forEach((item) => {
        const isPinned = !!Number(item.pinned || 0)
        const row = { id: item.id, title: String(item.title || '新的对话'), pinned: isPinned }
        if (isPinned) { pinned.push(row); return }
        const stamp = new Date(String(item.updated_at || '').replace(' ', 'T')).getTime()
        const safe = isNaN(stamp) ? 0 : stamp
        const bucket = safe >= today ? buckets[0] : safe >= week ? buckets[1] : buckets[2]
        bucket.items.push(row)
      })
      const groups: any[] = []
      if (pinned.length) groups.push({ key: 'pinned', label: '置顶', items: pinned })
      buckets.forEach((bucket) => { if (bucket.items.length) groups.push(bucket) })
      this.setData({ groups })
    },
    onSearchInput(e: any) { this.setData({ query: String(e.detail.value || '') }) },
    toggleSearch() { this.setData({ searching: !this.data.searching, query: '', openId: '' }) },
    clearQuery() { this.setData({ query: '' }) },
    // 列表滚动时收起已滑开的行，避免操作区悬在半空
    closeSwipe() { if (this.data.openId) this.setData({ openId: '' }) },
    onTouchStart(e: any) {
      const touch = (e.touches && e.touches[0]) || {}
      this.setData({ startX: Number(touch.clientX) || 0, startY: Number(touch.clientY) || 0 })
    },
    onTouchMove(e: any) {
      const touch = (e.touches && e.touches[0]) || {}
      const dx = (Number(touch.clientX) || 0) - this.data.startX
      const dy = (Number(touch.clientY) || 0) - this.data.startY
      // 竖向手势交给列表滚动，不让滑动误触发操作区
      if (Math.abs(dx) <= Math.abs(dy)) return
      const id = String(e.currentTarget.dataset.id || '')
      if (dx <= -SWIPE_THRESHOLD) {
        if (this.data.openId !== id) this.setData({ openId: id })
      } else if (dx >= SWIPE_THRESHOLD && this.data.openId) {
        this.setData({ openId: '' })
      }
    },
    // 松手后保持当前状态；tap 时再决定是收起还是进入会话
    onTouchEnd() { return },
    pick(e: any) {
      const id = String(e.currentTarget.dataset.id || '')
      if (this.data.openId) { this.setData({ openId: '' }); return }
      if (!id) return
      this.triggerEvent('pick', { id })
    },
    pin(e: any) {
      const id = String(e.currentTarget.dataset.id || '')
      const pinned = String(e.currentTarget.dataset.pinned || '0') === '1'
      if (!id) return
      this.setData({ openId: '' })
      this.triggerEvent('pin', { id, pinned: !pinned })
    },
    // 删除：只负责抛出事件，二次确认由页面弹窗完成
    remove(e: any) {
      const id = String(e.currentTarget.dataset.id || '')
      if (!id) return
      this.setData({ openId: '' })
      this.triggerEvent('remove', { id })
    },
    create() { this.triggerEvent('create') },
    close() { this.setData({ openId: '' }); this.triggerEvent('close') },
    noop() { return },
  },
})
