import { createKnowledge } from '../../services/api'
Page({
  data: { name:'', description:'', saving:false, error:'' },
  nameInput(e:any) { this.setData({ name:e.detail.value, error:'' }) },
  descriptionInput(e:any) { this.setData({ description:e.detail.value }) },
  async save() {
    const name=this.data.name.trim()
    if (!name || this.data.saving) return
    this.setData({ saving:true, error:'' })
    try {
      const item=await createKnowledge({ name, description:this.data.description.trim() })
      wx.redirectTo({ url:`/pages/knowledge-detail/index?id=${item.id}` })
    } catch { this.setData({ error:'创建未成功，请检查网络或稍后重试。填写的内容已保留。' }) }
    finally { this.setData({ saving:false }) }
  },
})
