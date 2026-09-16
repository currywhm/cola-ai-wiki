# cola 知识库 · 运营文案目录

这里放的是小程序里的运营内容，目前有两套：

- `tips/`：「使用技巧」图文，见下文。
- `legal/`：「关于 / 数据管理 / 隐私安全 / 小程序隐私保护指引 / 用户服务协议 /
  软件许可及服务协议 / 会员服务条款与权益说明」，说明与上线检查清单见
  [`legal/README.md`](legal/README.md)。

**内容归后端管：**小程序端只负责渲染，
所以改文案不需要动前端代码，也不需要重新提交小程序审核。

## 目录结构

```
content/
  legal/               合规文档（无配图，纯文本，方便逐字核对）
  tips/
    manifest.json        清单：版本号、分组、条目元信息（数组顺序 = 展示顺序）
    entries/             正文，一篇一个 Markdown
      01-upload.md
      02-organize.md
      ...
    assets/              配图（上线实际渲染的文件，PNG）
      src/               配图源文件（SVG，由脚本生成，方便二次修改）
```

## 怎么改内容

**只改文案**：直接编辑 `entries/*.md`，保存即生效（后端按文件时间戳自动失效缓存）。

**新增一篇**：

1. 在 `entries/` 下新建 Markdown，例如 `entries/09-search.md`
2. 在 `manifest.json` 对应分组的 `entries` 数组里加一项：

```json
{
  "id": "search",
  "title": "把资料找出来",
  "summary": "一句话说明这篇讲什么，会显示在列表里。",
  "icon": "sousuo",
  "cover": "tip-search.png",
  "body": "entries/09-search.md",
  "read_minutes": 2
}
```

**调整顺序**：改 `manifest.json` 里数组的顺序即可。

**改完记得升版本号**：把 `version` 改成新值（例如 `2026.09.20.1`），
小程序端按这个值判断是否需要刷新本地缓存。

**下线一篇**：把该项从 `manifest.json` 里删掉即可，Markdown 文件可以保留。

## 正文语法

只支持一个刻意收窄的子集，保证小程序端能用原生组件精确排版：

| 写法 | 渲染成 |
| --- | --- |
| `## 小标题` | 段落标题 |
| 直接写一行 | 正文段落 |
| `- 内容` | 圆点列表 |
| `1. 内容` | 带序号步骤（序号由后端重排，写错也不会错位） |
| `> 内容` | 浅底提示块 |
| `![图注](tip-upload.png)` | 配图 + 图注，图片必须放在 `assets/` |
| `**加粗**`、`` `等宽` `` | 行内强调 |

## 配图

配图由 `scripts/build_tips_assets.py` 生成，没有引用任何第三方素材，不存在版权问题。

```bash
python3 scripts/build_tips_assets.py                # 全部重建
python3 scripts/build_tips_assets.py tip-upload     # 只重建指定几张
```

脚本输出两份：`assets/src/<name>.svg`（可编辑源文件）和 `assets/<name>.png`（上线用，2 倍图）。
`qlmanage` / `sips` 只在本地生成图片时需要，生产服务器只需要托管已经提交的 PNG。

新增配图的流程：在脚本的 `ILLUSTRATIONS` 里加一个函数 -> 执行脚本 -> 提交 `assets/` 目录。

## 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/content/tips` | 列表：分组 + 条目元信息，不含正文，可长期缓存 |
| `GET /api/content/tips/{id}` | 单篇：元信息 + 解析好的结构化 blocks |
| `GET /api/content/assets/{文件名}` | 配图字节，带 `Cache-Control: max-age=86400` |

三个接口都不挂登录态：内容不含用户数据，且小程序的 `<image>` 无法携带
`Authorization` 头，配图必须能匿名取到。

上线前记得把 API 域名加入小程序后台的 **downloadFile 合法域名**，否则真机上配图会加载失败。
