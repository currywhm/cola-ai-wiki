// 新建 / 编辑「我的技能」。
// 规则：技能默认私有，只有自己能在对话里选中并注入；发布到技能广场后其他人才用得到，随时可以下架。
import { createSkill, deleteSkill, getSkill, publishSkill, Skill, updateSkill } from '../../services/api'

// 可选图标：与后端 SKILL_ICONS 白名单保持一致，避免出现前端能选、后端回落的情况
// 全部选单色线性图标：一套笔画、一个色阶，排在一起不会花
const ICONS = ['skill-node', 'knowledge-pick', 'book', 'ppt', 'image', 'sousuo', 'sliders', 'wangluo', 'atom', 'robot', 'liebiao', 'shuju', 'history', 'dui']

const NAME_MAX = 30
const SUMMARY_MAX = 60
const PROMPT_MAX = 4000

Page({
  data: {
    navHeight: 88,
    id: '',
    loading: false,
    saving: false,
    creating: false,
    published: false,
    name: '',
    summary: '',
    prompt: '',
    wechat: '',
    icon: 'skill-node',
    icons: ICONS,
  },
  onLoad(query: any) {
    // 新建页保留返回键，编辑页顶部左侧同样只放返回，避免和胶囊按钮抢位置
    this.measureNav()
    const id = String((query && query.id) || '')
    if (!id) return
    this.setData({ id, loading: true })
    getSkill(id).then((skill: Skill) => {
      this.setData({
        loading: false,
        name: skill.name || '',
        summary: skill.summary || '',
        prompt: skill.prompt || '',
        wechat: skill.developer_wechat || '',
        icon: skill.icon || 'skill-node',
        published: !!skill.published,
      })
      if (!skill.is_owner) {
        // 别人的技能只读：直接退回，避免改了报错
        wx.showToast({ title: '只能编辑自己的技能', icon: 'none' })
        setTimeout(() => this.goBack(), 900)
      }
    }).catch((error: any) => {
      this.setData({ loading: false })
      wx.showToast({ title: error.message || '技能不存在或已删除', icon: 'none' })
      setTimeout(() => this.goBack(), 900)
    })
  },
  // 顶部不留标题栏：只避让状态栏与胶囊按钮
  measureNav() {
    try {
      const api = wx as any
      const info = api.getWindowInfo ? api.getWindowInfo() : wx.getSystemInfoSync()
      const rect = wx.getMenuButtonBoundingClientRect()
      const status = info.statusBarHeight || 20
      const valid = rect && rect.height > 0 && rect.top >= status
      const height = valid ? Math.max(44, (rect.top - status) * 2 + rect.height) : 44
      this.setData({ navHeight: status + height })
    } catch (e) { this.setData({ navHeight: 88 }) }
  },
  goBack() {
    wx.navigateBack({ delta: 1, fail: () => { wx.switchTab({ url: '/pages/mine/index' }) } })
  },
  noop() { return },
  onName(e: any) { this.setData({ name: String(e.detail.value || '').slice(0, NAME_MAX) }) },
  onSummary(e: any) { this.setData({ summary: String(e.detail.value || '').slice(0, SUMMARY_MAX) }) },
  onPrompt(e: any) { this.setData({ prompt: String(e.detail.value || '').slice(0, PROMPT_MAX) }) },
  onWechat(e: any) { this.setData({ wechat: String(e.detail.value || '').slice(0, 40) }) },
  pickIcon(e: any) { this.setData({ icon: String(e.currentTarget.dataset.icon || 'skill-node') }) },
  // 保存前的本地校验：名称和指令是必填，其余可以留空
  validate(): { name: string; summary: string; prompt: string; developer_wechat: string; icon: string } | null {
    const name = this.data.name.trim()
    const prompt = this.data.prompt.trim()
    if (!name) { wx.showToast({ title: '请填写技能名称', icon: 'none' }); return null }
    if (!prompt) { wx.showToast({ title: '请填写技能指令', icon: 'none' }); return null }
    return { name, summary: this.data.summary.trim(), prompt, developer_wechat: this.data.wechat.trim(), icon: this.data.icon }
  },
  save(): Promise<Skill | null> {
    const form = this.validate()
    if (!form) return Promise.resolve(null)
    this.setData({ saving: true })
    const task = this.data.id ? updateSkill(this.data.id, form) : createSkill(form)
    return task.then((skill) => {
      this.setData({ saving: false })
      return skill
    }).catch((error: any) => {
      this.setData({ saving: false })
      wx.showToast({ title: error.message || '保存失败，请稍后重试', icon: 'none' })
      return null
    })
  },
  // 新建时先问一句：保存后要不要顺手发布到广场
  submit() {
    if (this.data.saving) return
    if (!this.data.id) {
      const form = this.validate()
      if (!form) return
      this.setData({ creating: true })
      return
    }
    this.save().then((skill) => {
      if (!skill) return
      wx.showToast({ title: '已保存', icon: 'success' })
      setTimeout(() => this.goBack(), 600)
    })
  },
  submitPrivate() {
    this.setData({ creating: false })
    this.save().then((skill) => {
      if (!skill) return
      wx.showToast({ title: '已保存，只有你能用', icon: 'none' })
      setTimeout(() => this.goBack(), 700)
    })
  },
  submitAndPublish() {
    this.setData({ creating: false })
    this.save().then((skill) => {
      if (!skill) return
      publishSkill(skill.id, true).then(() => {
        wx.showToast({ title: '已发布到技能广场', icon: 'success' })
        setTimeout(() => this.goBack(), 700)
      }).catch((error: any) => {
        // 发布失败不影响技能本身：已经保存成私有技能
        wx.showToast({ title: error.message || '已保存，但发布失败', icon: 'none' })
        setTimeout(() => this.goBack(), 900)
      })
    })
  },
  // 下架 / 重新发布：只改可见性，技能内容不动
  togglePublish() {
    if (this.data.saving || !this.data.id) return
    const published = !this.data.published
    this.setData({ saving: true })
    publishSkill(this.data.id, published).then((skill) => {
      this.setData({ saving: false, published: !!skill.published })
      wx.showToast({ title: published ? '已发布到技能广场' : '已从广场下架', icon: 'none' })
    }).catch((error: any) => {
      this.setData({ saving: false })
      wx.showToast({ title: error.message || '操作失败，请稍后重试', icon: 'none' })
    })
  },
  // 删除是不可恢复的：二次确认后才真正删，同时提醒已发布的内容也会从广场消失
  remove() {
    if (!this.data.id || this.data.saving) return
    wx.showModal({
      title: '删除这个技能',
      content: this.data.published ? '删除后它会从技能广场一起消失，且无法恢复。' : '删除后无法恢复，之后需要重新创建。',
      confirmText: '删除',
      confirmColor: '#d94545',
      success: (res) => {
        if (!res.confirm) return
        this.setData({ saving: true })
        deleteSkill(this.data.id).then(() => {
          this.setData({ saving: false })
          wx.showToast({ title: '已删除', icon: 'success' })
          setTimeout(() => this.goBack(), 600)
        }).catch((error: any) => {
          this.setData({ saving: false })
          wx.showToast({ title: error.message || '删除失败，请稍后重试', icon: 'none' })
        })
      },
    })
  },
})
