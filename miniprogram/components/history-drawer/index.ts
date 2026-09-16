// 历史对话抽屉：会话页左上角「折叠」图标拉出的左抽屉，用来切换历史会话 / 新建对话。
// 数据全部来自后端 /api/conversations（按用户 + 知识库 + 文件夹收窄），组件本身不缓存任何内容。

interface ConversationItem {
  id: string
  title?: string
  updated_at?: string
  knowledge_id?: string
  folder_id?: string
}

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
        this.setData({ searching: false, query: '' })
        this.buildGroups()
      }
    },
  },
  methods: {
    // 今天 / 近 7 天 / 更早：按后端返回的 updated_at 分组，搜索词只过滤标题
    buildGroups() {
      const keyword = String(this.data.query || '').trim().toLowerCase()
      const source = ((this.data.items || []) as ConversationItem[]).filter((item) => !keyword || String(item.title || '').toLowerCase().includes(keyword))
      const today = startOfToday()
      const week = today - 6 * 86400000
      const buckets = [
        { key: 'today', label: '今天', items: [] as any[] },
        { key: 'week', label: '近7天', items: [] as any[] },
        { key: 'older', label: '更早', items: [] as any[] },
      ]
      source.forEach((item) => {
        const stamp = new Date(String(item.updated_at || '').replace(' ', 'T')).getTime()
        const safe = isNaN(stamp) ? 0 : stamp
        const bucket = safe >= today ? buckets[0] : safe >= week ? buckets[1] : buckets[2]
        bucket.items.push({ id: item.id, title: String(item.title || '新的对话') })
      })
      this.setData({ groups: buckets.filter((bucket) => bucket.items.length) })
    },
    onSearchInput(e: any) { this.setData({ query: String(e.detail.value || '') }) },
    toggleSearch() { this.setData({ searching: !this.data.searching, query: '' }) },
    clearQuery() { this.setData({ query: '' }) },
    pick(e: any) { this.triggerEvent('pick', { id: String(e.currentTarget.dataset.id || '') }) },
    // 长按删除：历史对话存在服务端，删除后不可恢复，由页面弹窗二次确认
    remove(e: any) { this.triggerEvent('remove', { id: String(e.currentTarget.dataset.id || '') }) },
    create() { this.triggerEvent('create') },
    close() { this.triggerEvent('close') },
    noop() { return },
  },
})
