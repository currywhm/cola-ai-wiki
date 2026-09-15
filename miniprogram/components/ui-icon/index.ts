const ICON_ROOT = '/assets/icons/cola-set/'

type IconConfig = { file: string; transform?: string }

const ICONS: Record<string, IconConfig> = {
  // Direct matches from the supplied icon collection.
  back: { file: 'fanhui.svg' },
  fanhui: { file: 'fanhui.svg' },
  'chevron-left': { file: 'fanhui.svg' },
  search: { file: 'sousuo.svg' },
  sousuo: { file: 'sousuo.svg' },
  plus: { file: 'tianjia.svg' },
  tianjia: { file: 'tianjia.svg' },
  menu: { file: 'liebiao.svg' },
  liebiao: { file: 'liebiao.svg' },
  more: { file: 'liebiao.svg' },
  compass: { file: 'discover-custom.svg' },
  faxian: { file: 'discover-custom.svg' },
  web: { file: 'scope-web.svg' },
  wangluo: { file: 'wangluo.svg' },
  shield: { file: 'anquanbaozhang.svg' },
  anquanbaozhang: { file: 'anquanbaozhang.svg' },
  data: { file: 'shuju.svg' },
  shuju: { file: 'shuju.svg' },
  info: { file: 'dengpao.svg' },
  dengpao: { file: 'dengpao.svg' },
  agreement: { file: 'jieshao.svg' },
  jieshao: { file: 'jieshao.svg' },
  user: { file: 'guanyu.svg' },
  guanyu: { file: 'guanyu.svg' },
  folder: { file: 'folder.svg' },
  'folder-green': { file: 'folder-green.svg' },
  'folder-add': { file: 'folder-add-custom.svg' },
  file: { file: 'upload-doc.svg' },
  knowledge: { file: 'scope-knowledge.svg' },
  response: { file: 'dui.svg' },
  dui: { file: 'dui.svg' },
  check: { file: 'dui.svg' },
  spark: { file: 'dengpao.svg' },
  atom: { file: 'atom.svg' },
  vip: { file: 'vip.svg' },
  'knowledge-mode': { file: 'scope-knowledge.svg' },
  copy: { file: 'copy.svg' },
  share: { file: 'share.svg' },
  pin: { file: 'pin.svg' },
  'pin-white': { file: 'pin-white.svg' },
  home: { file: 'guanyu.svg' },
  help: { file: 'guanyu.svg' },
  settings: { file: 'liebiao.svg' },
  trash: { file: 'trash.svg' },
  tag: { file: 'tag.svg' },
  upload: { file: 'document-add.svg' },
  'wechat-file': { file: 'wechat-custom.svg' },
  gongzhonghao: { file: 'gongzhonghao.svg' },
  'wechat-article': { file: 'gongzhonghao.svg' },
  image: { file: 'album-custom.svg' },
  'image-blue': { file: 'image-blue.svg' },
  camera: { file: 'camera-custom.svg' },
  filter: { file: 'liebiao.svg' },
  chat: { file: 'guanyu.svg' },
  send: { file: 'send.svg' },
  stop: { file: 'stop.svg' },
  tingzhi: { file: 'stop.svg' },
  history: { file: 'history.svg' },
  arrow: { file: 'fanhui.svg', transform: 'transform:scaleX(-1);' },
  'chevron-right': { file: 'fanhui.svg', transform: 'transform:scaleX(-1);' },
  'chevron-down': { file: 'fanhui.svg', transform: 'transform:rotate(-90deg);' },
  close: { file: 'tianjia.svg', transform: 'transform:rotate(45deg);' },
}

function resolveIcon(name: string): IconConfig {
  return ICONS[name] || ICONS.file
}

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
