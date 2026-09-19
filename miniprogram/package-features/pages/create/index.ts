import { createKnowledge, uploadKnowledgeAvatar } from '../../../services/api'
import { ensurePrivacyAuthorized, isPrivacyScopeError, openPrivacyContract, warnPrivacyRequired } from '../../../services/privacy'
Page({
  // quotaBlocked：服务端因为会员名额拒绝创建时，页面上多给一个「查看会员服务」的出口，而不是只留一句看不懂的报错。
  data: { name:'', description:'', saving:false, error:'', quotaBlocked:false, from:'', avatarPreview:'' },
  onLoad(query: any) {
    // from=library：从知识库页的新建入口进来，建好后回到知识库页并默认选中它
    this.setData({ from: String((query && query.from) || '') })
  },
  nameInput(e:any) { this.setData({ name:e.detail.value, error:'', quotaBlocked:false }) },
  descriptionInput(e:any) { this.setData({ description:e.detail.value }) },
  chooseAvatar() {
    if (this.data.saving) return
    ensurePrivacyAuthorized().then((granted) => {
      if (!granted) { warnPrivacyRequired('从个人相册选择知识库头像'); return }
      const media = wx as any
      const accept = (path: string) => { if (path) this.setData({ avatarPreview: path }) }
      const failed = (error: any) => {
        if (String(error?.errMsg || '').includes('cancel')) return
        // 「api scope is not declared in the privacy agreement」= 后台《用户隐私保护指引》
        // 还没勾选「收集你选中的照片或视频信息」，这时微信不会弹官方授权弹窗，
        // 界面上也不会出现任何授权入口，所以要给一条现在就能走通的路：从微信文件里选图。
        if (isPrivacyScopeError(error)) {
          wx.showModal({
            title: '打不开相册',
            content: '微信没有返回相册权限（可能刚才点了拒绝，也可能小程序的隐私指引还没包含相册）。可以先从微信文件里选一张图片当头像，或点「看指引」了解我们要收集什么。',
            confirmText: '从微信文件选',
            cancelText: '看指引',
            success: (result) => {
              if (result.confirm) this.chooseAvatarFromFile()
              else this.openPrivacyGuide()
            },
          })
          return
        }
        wx.showToast({ title: '需要个人相册权限才能选择知识库头像', icon: 'none' })
      }
      if (typeof media.chooseMedia === 'function') {
        media.chooseMedia({
          count: 1, mediaType: ['image'], sourceType: ['album'], sizeType: ['compressed'],
          success: (result: any) => accept(String(result?.tempFiles?.[0]?.tempFilePath || '')),
          fail: failed,
        })
        return
      }
      wx.chooseImage({ count: 1, sizeType: ['compressed'], sourceType: ['album'], success: (result) => accept(String(result.tempFilePaths?.[0] || '')), fail: failed })
    })
  },
  // 相册权限拿不到时的兜底：微信文件选择是另一个隐私项，通常可用
  chooseAvatarFromFile() {
    wx.chooseMessageFile({
      count: 1, type: 'image',
      success: (pick) => {
        const item = pick.tempFiles && pick.tempFiles[0]
        if (item && item.path) this.setData({ avatarPreview: item.path })
      },
      fail: (error: any) => {
        if (String(error?.errMsg || '').includes('cancel')) return
        wx.showToast({ title: '没有选到图片，可以稍后在知识库里再设置头像', icon: 'none' })
      },
    })
  },
  openPrivacyGuide() {
    if (!openPrivacyContract()) wx.navigateTo({ url: '/package-features/pages/legal/index?type=guide' })
  },
  async save() {
    const name=this.data.name.trim()
    if (!name || this.data.saving) return
    this.setData({ saving:true, error:'' })
    try {
      const item=await createKnowledge({ name, description:this.data.description.trim() })
      if (this.data.avatarPreview) {
        wx.showLoading({ title: '保存头像', mask: true })
        try { await uploadKnowledgeAvatar(item.id, this.data.avatarPreview) }
        catch (error: any) { wx.showToast({ title: error?.message || '知识库已创建，头像稍后可重新设置', icon: 'none' }) }
        finally { wx.hideLoading() }
      }
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
