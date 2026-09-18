// 图标名 → 素材文件的映射放在 utils/icons，页面里需要直接用 <image> 画图标时读同一份。
import { ICON_ROOT, ICONS, resolveIcon } from '../../utils/icons'

Component({
  properties: {
    name: { type: String, value: 'folder' },
    size: { type: Number, value: 24 },
    // 传入 rpx 后，size 按 rpx 解释：图标随屏幕宽度等比缩放，避免小屏溢出/大屏偏小
    rpx: { type: Boolean, value: false },
  },
  data: {
    iconFile: `${ICON_ROOT}${ICONS.folder.file}`,
    iconTransform: '',
    sizeStyle: 'width:24px;height:24px;',
  },
  lifetimes: {
    attached() {
      this.syncIcon()
    },
  },
  observers: {
    name() {
      this.syncIcon()
    },
    'size, rpx'() {
      this.syncSize()
    },
  },
  methods: {
    syncSize() {
      const unit = this.data.rpx ? 'rpx' : 'px'
      const value = Number(this.data.size) || 24
      this.setData({ sizeStyle: `width:${value}${unit};height:${value}${unit};` })
    },
    syncIcon() {
      const config = resolveIcon(String(this.data.name || 'folder'))
      this.setData({
        iconFile: `${ICON_ROOT}${config.file}`,
        iconTransform: config.transform || '',
      })
    },
  },
})
