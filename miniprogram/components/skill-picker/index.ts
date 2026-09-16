// 技能选择面板：技能广场（所有人可用）与我的技能（用户级隔离）。
// 只有在这里被选中的技能才会随本轮对话注入后端：内置技能走技能包，我的技能走技能指令。
// 面板只负责列表、点赞收藏与发布入口；支持多选，选中结果通过 select 事件交给页面，
// 由页面负责持久化（本地 + 服务端），面板本身不保存状态。
import { favoriteSkill, getSkills, likeSkill, publishSkill, Skill } from '../../services/api'

// 搜索防抖：连续输入时不打请求，停手后再查
const SEARCH_DEBOUNCE = 260

Component({
  properties: {
    visible: { type: Boolean, value: false },
    // 当前已选中的技能 id 列表：由页面持有，面板只做高亮与切换（可多选）
    selected: { type: Array, value: [] },
  },
  data: {
    scope: 'market' as 'market' | 'mine',
    query: '',
    list: [] as Skill[],
    loading: false,
    failed: false,
    // 面板的确定高度（px）：scroll-view 必须有确定高度才会滚动，
    // 只给 max-height 时列表会被内容撑开、划不动。
    panelHeight: 0,
    selectedCount: 0,
  },
  observers: {
    // 页面侧修改选中项（清空、从存储恢复）时同步高亮
    selected(value: any) {
      this.setData({ selectedCount: (value || []).length }, () => this.markSelected())
    },
    visible(value: boolean) {
      if (!value) return
      // 先按当前列表算一次高度（避免刚弹出时先塌一下），拉完列表再校准
      this.layout()
      this.reload()
    },
  },
  methods: {
    noop() { return },
    // 面板高度按「行数」估算后写成确定值：列表用 flex 撑满剩余空间。
    // 估多一档只会在列表尾部多一点留白，估少一档只会多滚一点，都能用。
    layout() {
      const api: any = wx as any
      const info: any = api.getWindowInfo ? api.getWindowInfo() : wx.getSystemInfoSync()
      const winW = Number(info && info.windowWidth) || 375
      const winH = Number(info && info.windowHeight) || 667
      const ratio = winW / 750
      const rows = Math.max(1, (this.data.list || []).length + (this.data.scope === 'mine' ? 1 : 0))
      // 492rpx：标题 + 两栏 + 搜索 + 底部说明的固定高度；205rpx：一张技能卡（含间距）
      const fixed = 492 * ratio
      const listPx = rows * 205 * ratio
      const panel = Math.min(winH * 0.84, Math.max(winH * 0.5, fixed + listPx))
      this.setData({ panelHeight: Math.max(320, Math.round(panel)) })
    },
    close() { this.triggerEvent('close') },
    switchScope(e: any) {
      const scope = e.currentTarget.dataset.scope === 'mine' ? 'mine' : 'market'
      if (scope === this.data.scope) return
      this.setData({ scope, query: '' }, () => this.reload())
    },
    onSearch(e: any) {
      this.setData({ query: String(e.detail.value || '') })
      const pending = (this as any).searchTimer
      if (pending) clearTimeout(pending)
      ;(this as any).searchTimer = setTimeout(() => {
        (this as any).searchTimer = null
        this.reload()
      }, SEARCH_DEBOUNCE)
    },
    clearSearch() {
      this.setData({ query: '' }, () => this.reload())
    },
    reload() {
      const sequence = ((this as any).sequence || 0) + 1
      ;(this as any).sequence = sequence
      this.setData({ loading: true, failed: false })
      getSkills(this.data.scope, this.data.query.trim()).then((list) => {
        // 只认最后一次请求的结果，避免切页时旧响应覆盖新列表
        if ((this as any).sequence !== sequence) return
        this.setData({ list: this.marked(list || []), loading: false }, () => this.layout())
      }).catch(() => {
        if ((this as any).sequence !== sequence) return
        this.setData({ list: [], loading: false, failed: true }, () => this.layout())
      })
    },
    // 列表需要逐项携带选中态：WXML 里不能对数组做 indexOf
    marked(list: Skill[]): any[] {
      const selected = this.data.selected || []
      return list.map((item) => ({ ...item, _on: selected.indexOf(item.id) !== -1 }))
    },
    markSelected() {
      if (!this.data.list.length) return
      this.setData({ list: this.marked(this.data.list as Skill[]) })
    },
    // 就地更新一条技能的展示数据（点赞 / 收藏 / 发布都走这里）
    patch(id: string, changes: Record<string, any>) {
      const list = this.data.list.map((item) => (item.id === id ? { ...item, ...changes } : item))
      this.setData({ list })
    },
    skillOf(e: any): Skill | undefined {
      const id = String((e.currentTarget && e.currentTarget.dataset.id) || '')
      return this.data.list.find((item) => item.id === id)
    },
    // 多选：点一下切换选中状态，面板不关闭，由页面的 selected 回流高亮
    pick(e: any) {
      const skill = this.skillOf(e)
      if (!skill) return
      const selected = this.data.selected || []
      const on = selected.indexOf(skill.id) === -1
      this.setData({ list: this.marked(this.data.list as Skill[]).map((item: any) => (item.id === skill.id ? { ...item, _on: on } : item)) })
      this.triggerEvent('select', { skill, selected: on })
    },
    toggleFavorite(e: any) {
      const skill = this.skillOf(e)
      if (!skill) return
      const active = !skill.favorited
      this.patch(skill.id, { favorited: active, favorite_count: Math.max(0, skill.favorite_count + (active ? 1 : -1)) })
      favoriteSkill(skill.id, active).then((result) => {
        this.patch(skill.id, { favorited: result.active, favorite_count: result.count })
      }).catch(() => {
        this.patch(skill.id, { favorited: skill.favorited, favorite_count: skill.favorite_count })
        wx.showToast({ title: '操作失败，请稍后重试', icon: 'none' })
      })
    },
    toggleLike(e: any) {
      const skill = this.skillOf(e)
      if (!skill) return
      const active = !skill.liked
      this.patch(skill.id, { liked: active, like_count: Math.max(0, skill.like_count + (active ? 1 : -1)) })
      likeSkill(skill.id, active).then((result) => {
        this.patch(skill.id, { liked: result.active, like_count: result.count })
      }).catch(() => {
        this.patch(skill.id, { liked: skill.liked, like_count: skill.like_count })
        wx.showToast({ title: '操作失败，请稍后重试', icon: 'none' })
      })
    },
    togglePublish(e: any) {
      const skill = this.skillOf(e)
      if (!skill) return
      const published = !(skill.visibility === 'public')
      publishSkill(skill.id, published).then((updated) => {
        this.patch(skill.id, { visibility: updated.visibility, published: updated.published })
        wx.showToast({ title: published ? '已发布到技能广场' : '已从广场下架', icon: 'none' })
      }).catch((error: any) => {
        wx.showToast({ title: error.message || '发布失败，请稍后重试', icon: 'none' })
      })
    },
    createSkill() { this.triggerEvent('create') },
    editSkill(e: any) {
      const skill = this.skillOf(e)
      if (skill) this.triggerEvent('edit', { id: skill.id })
    },
  },
})
