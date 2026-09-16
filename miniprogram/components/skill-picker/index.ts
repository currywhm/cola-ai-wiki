// 技能选择面板：技能广场（所有人可用）与我的技能（用户级隔离）。
// 只有在这里被选中的技能才会随本轮对话注入后端：内置技能走技能包，我的技能走技能指令。
// 面板只负责列表、点赞收藏与发布入口，选中结果通过 select 事件交给页面。
import { favoriteSkill, getSkills, likeSkill, publishSkill, Skill } from '../../services/api'

// 搜索防抖：连续输入时不打请求，停手后再查
const SEARCH_DEBOUNCE = 260

Component({
  properties: {
    visible: { type: Boolean, value: false },
    // 当前已选中的技能 id：由页面持有，面板只做高亮
    selected: { type: String, value: '' },
  },
  data: {
    scope: 'market' as 'market' | 'mine',
    query: '',
    list: [] as Skill[],
    loading: false,
    failed: false,
  },
  observers: {
    visible(value: boolean) {
      if (value) this.reload()
    },
  },
  methods: {
    noop() { return },
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
        this.setData({ list: list || [], loading: false })
      }).catch(() => {
        if ((this as any).sequence !== sequence) return
        this.setData({ list: [], loading: false, failed: true })
      })
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
    pick(e: any) {
      const skill = this.skillOf(e)
      if (skill) this.triggerEvent('select', skill)
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
