import { createKnowledge } from '../../../services/api'
Page({
  // quotaBlocked：服务端因为会员名额拒绝创建时，页面上多给一个「查看会员服务」的出口，而不是只留一句看不懂的报错。
  data: { name:'', description:'', saving:false, error:'', quotaBlocked:false, from:'' },
  onLoad(query: any) {
    // from=library：从知识库页的新建入口进来，建好后回到知识库页并默认选中它
    this.setData({ from: String((query && query.from) || '') })
  },
  nameInput(e:any) { this.setData({ name:e.detail.value, error:'', quotaBlocked:false }) },
  descriptionInput(e:any) { this.setData({ description:e.detail.value }) },
  async save() {
    const name=this.data.name.trim()
    if (!name || this.data.saving) return
    this.setData({ saving:true, error:'' })
    try {
      const item=await createKnowledge({ name, description:this.data.description.trim() })
      if (this.data.from) {
        // 回到知识库页，并且默认选中刚建好的这个库（不再回到「选择知识库」弹窗）
        wx.setStorageSync('kb_focus', item.id)
        // 不论从知识库页还是「问AI」进来，建好都落到知识库页并默认选中新库：
        // switchTab 会把创建页关掉，kb_focus 由知识库页 onShow 时读走
        wx.switchTab({ url:'/pages/chat/index', fail: () => wx.navigateBack({ fail: () => wx.redirectTo({ url:`/package-features/pages/knowledge-detail/index?id=${item.id}` }) }) })
      } else {
        wx.redirectTo({ url:`/package-features/pages/knowledge-detail/index?id=${item.id}` })
      }
    } catch (error: any) {
      const status = Number((error && error.statusCode) || 0)
      const message = String((error && error.message) || '').trim()
      // 403 基本都是名额 / 状态限制：照服务端原话告诉用户，并给一个升级出口
      const quota = status === 403 && /会员|资料库|知识库|名额|试用/.test(message)
      this.setData({ error: message || '创建未成功，请检查网络或稍后重试。填写的内容已保留。', quotaBlocked: quota })
    }
    finally { this.setData({ saving:false }) }
  },
  openMembership() { wx.switchTab({ url: '/pages/mine/index' }) },
})
