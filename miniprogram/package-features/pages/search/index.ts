import { searchKnowledge } from '../../../services/api'

Page({
 data:{ query:'', searched:false, searching:false, error:'', results:[] as any[] },
 input(e:any) {this.setData({query:e.detail.value})},
 clearQuery() {this.setData({query:'', searched:false, results:[], error:''})},
 async submit() {
  if(this.data.searching || !this.data.query.trim())return
  this.setData({searching:true,error:''})
  try {const results=await searchKnowledge(this.data.query.trim());this.setData({results,searched:true})}
  catch {this.setData({error:'搜索未完成，请检查网络后重试。',searched:false,results:[]})}
  finally {this.setData({searching:false})}
 },
 openDocument(e:any){wx.navigateTo({url:`/package-features/pages/document/index?id=${e.currentTarget.dataset.id}`})},
 openMine(){wx.switchTab({url:'/pages/mine/index'})},
 back(){wx.navigateBack()},
})
