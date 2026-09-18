# 知库微信小程序

本仓库同时包含微信小程序端和后端：`miniprogram/` 是前端源码，`server/` 是 FastAPI 后端与微信云托管部署源码。

知库是面向个人资料的智能阅读小程序：文件上传后解析为文本切片，支持关键词检索、引用来源和基于资料库的流式问答。前端为微信原生 TypeScript/WXML/WXSS，服务端为 FastAPI。

## 隐私授权（《小程序用户隐私保护指引》）

登录协议同意在 `pages/login/index` 收口，微信系统隐私授权在 `services/privacy.ts` 收口；两者职责分开，不混用。

- 登录门禁：`pages/login/index` 是小程序唯一入口。没有登录令牌时，`ensureAuth()` 只回登录页，不再静默调用 `wx.login`；问 AI、知识库、最近、我的等页面均不能绕过登录。
- 登录同意：登录页微信按钮下方需要勾选《服务协议》《隐私政策》《AI 隐私政策》。已勾选时直接登录并进入问 AI；未勾选时先弹隐私提示层，点“同意”后再继续登录。
- 默认账号：服务端按微信 OpenID 创建或关联账号，默认昵称为“微信用户”、头像为产品默认图。登录过程不调用 `wx.getUserProfile` / `wx.getUserInfo` / `<open-data>`，不会假装读取真实微信资料。
- 可选设置：“我的 → 账号设置”保留微信官方 `<button open-type="chooseAvatar">` 和 `<input type="nickname">`，用户主动点击后选择并保存；未设置时继续显示默认资料。
- 系统隐私弹窗：`services/privacy.ts` 不注册 `wx.onNeedPrivacyAuthorization`，相册、拍照、微信文件等隐私接口交给微信官方弹窗处理；未同意时对应能力不可用。
- 微信后台按实际能力申报：「收集你的昵称、头像」（仅账号设置主动选择）、「收集你选中的照片或视频信息」、「收集你选中的文件」。位置、麦克风、通讯录、日历、微信运动不申请。
- 退出登录：“我的 → 退出登录”先弹底部确认层，确认后调用后端退出、清理令牌并回到登录页；服务端资料不随退出自动删除。
- 账号设置与导入文件弹层打开时会收起原生 tabBar，避免遮住底部按钮。


## 项目结构

- `miniprogram/`：微信小程序端，已在微信开发者工具中加载验证。
- `server/`：可部署到外部服务器的 API 服务。
- `server/Dockerfile`、`server/docker-compose.yml`：生产部署入口。

## 小程序端编码约束

不要在小程序源码（`miniprogram/**/*.ts`、`*.js`）里用**数组解构赋值**，比如 `Promise.all([...]).then(([a, b]) => {})`、`const [x, y] = list`。

开发者工具把 ES6 转 ES5 时会通过 `loadBabelMod()` 注入 `@babel/runtime` 的 helper；本机工具版本注入不了 `slicedToArray`，只要有页面用到数组解构，该页会在**模块加载阶段**直接报 `module '@babel/runtime/helpers/arrayWithHoles.js' is not defined`，`Page({...})` 不会执行，页面只剩下底部 tabBar（白屏）。`pages/chat`、`pages/mine`、`pages/tips` 都踩过这个坑。

改成下标取值即可：

```ts
Promise.all([getMe(), getKnowledge()]).then((pair) => {
  const user = pair[0]
  const knowledge = pair[1]
})
```

提交前跑 `python3 scripts/check_mp_helpers.py` 自检，退出码非 0 表示存在风险写法。

## 开发者工具编译产物的运行时依赖

`miniprogram/@swc/runtime/` 不是业务代码，是编译产物的运行时补丁，**不要删**。开发者工具的 TypeScript 插件在部分文件中会走 SWC 转换，产出的代码是外部 helper 形式：

```js
var _async_to_generator = require("../../@swc/runtime/_async_to_generator")
```

而工具不会把这个运行时打进小程序包，于是页面在模块加载阶段就抛 `module '@swc/runtime/_async_to_generator.js' is not defined`，`Page({...})` 不执行，画面只剩底部 tabBar（表现就是「页面打不开 / 白屏」）。补齐同名 helper 后 require 能解析到真实模块，页面即恢复正常；`project.config.json` 的 `ignoreUploadUnusedFiles` 保持 `false`，避免上传时把只被编译产物引用的 helper 当无用文件裁掉。

以后再遇到同类报错，按报错里的模块名在 `miniprogram/@swc/runtime/` 补一个同名文件即可（调用约定固定为 `helper._(...)`，所以模块要同时导出 `module.exports._`）。

## 自定义组件 wxss 的选择器约束

组件的 `wxss` 里**不能出现标签选择器、ID 选择器、属性选择器**。写了工具会打印：

```
[pages/ask/index] Some selectors are not allowed in component wxss, including tag name selectors, ID selectors, and attribute selectors.
```

这条规则被丢弃后**不会自动降级**，而是整条规则失效，页面直接变形（常见表现是按钮丢边框、内容挤到中间、文字居中贴左）。

想覆盖 `<button>` 样式时，不能写 `button.hd-new {}`，也不能只写 `.hd-new {}`。基础库给按钮预设的是 `wx-button[class]`，特异性是 `(0,1,1)`，单类名 `(0,1,0)` 压不住。正确做法是**把类名连写两遍**，把特异性提到 `(0,2,0)`：

```css
/* 等价于原来的 button.hd-new，但仍是合法的类选择器 */
.hd-new.hd-new { border: 1rpx solid #ececec; border-radius: 999rpx; text-align: left; }
.hd-new.hd-new::after { border: 0; }
```

`app-nav`、`history-drawer`、`pick-bar`、`plan-card`、`share-sheet`、`skill-picker` 六个组件的按钮样式都已按这个写法改过（共 76 处），新增组件时照此处理。

## 图片一律先落到本地再上屏

`<image>` 已经不支持 `http://`，直接绑 http 地址会每次都打印：

```
[Component] <wx-image>: 图片链接 http://127.0.0.1:8765/api/content/assets/xxx.png 不再支持 HTTP 协议，请升级到 HTTPS
```

`services/media.ts` 的 `localizeImage` 负责把远端图片下载成本地临时文件（渲染成 `http://usr/...`）。关键是不能「先 setData 远端地址、再异步替换」，那样每帧都会刷警告。正确顺序是**先留空、拿到本地路径再回填**：

```ts
list.map((entry) => ({ ...entry, srcSource: contentAssetUrl(entry.cover), src: '' }))
// 本地化完成后
this.setData({ list: localized })
```

`pages/mine/index.ts`、`pages/tips/index.ts` 已按这个写法落地（`srcSource` / `coverSource` 存原始地址，`src` / `cover` 只放本地路径）。

## 积分计费（额度口径）

额度单位是**积分**，不是问答次数：一次问答按真实 token 用量扣分，长资料、长回答、深度思考、联网检索、生成文件都比普通问答扣得多。

- 真实成本按 deepseek-flash 官方单价算（元/百万 tokens）：输入命中缓存 0.04、输入未命中 2.00、输出 8.00；官方高峰时段为北京时间周一至周五 9:00-12:00 与 14:00-18:00，其余时段半价，服务端按请求时刻自动取价。
- 用户价 = 真实成本 × 1.5（`CREDIT_MARKUP`），积分 = 用户价 ÷ 0.001 元（`CREDIT_UNIT_YUAN`），四舍五入且一轮至少 1 分。
- 加价倍率恒定 1.5 倍，但**用户可见的积分会随时段浮动**——同一段对话空闲时段约 3 分、高峰时段约 7 分。这是刻意保留的口径（成本完全转嫁、毛利率恒定），对外文案必须写明「积分随官方计价时段浮动」，不能写成「不随时段变化」。
- 一轮普通问答约 3 积分。月度额度：免费试用期 600、试用结束后 150、Plus 3000、Pro 15000，按自然月归零、不结转。
- 每轮结算写入 `usage_logs`（用户、会话、消息、模型、四类 token、成本、积分、时间），所以「这一分怎么扣的」能回到具体轮次与 token；`/api/me` 返回 `quota.credits_limit/credits_used/credits_left`，`/api/pay/plans` 返回 `monthly_credits`，问答流结束时 `done` 事件带 `credits / credits_used / credits_limit`。
- 计费口径同时在 `server/app/services/credits.py`（计算）与合规/技巧文案（`server/content/legal/`、`server/content/tips/`）里出现，改单价或额度时两边必须一起改，并重跑 `python scripts/import_content.py`。

## 本地联调

```bash
cd server
python3 scripts/manage.py setup
.venv/bin/python scripts/manage.py start
```

小程序开发配置当前使用 `http://127.0.0.1:8765`，接口文档在 `http://127.0.0.1:8765/docs`。`project.private.config.json` 关闭了开发工具的合法域名校验。真机与正式版必须改成外部服务器的 HTTPS 域名，并在微信公众平台配置 request、uploadFile、downloadFile 服务器域名；上线前将 `urlCheck` 恢复为 `true`。

## 外部服务器部署

1. 准备 Linux 服务器、域名和 HTTPS 证书，创建 `/opt/cola-ai-wiki`。
2. 上传 `server/dist/zhi-reader-api.tar.gz` 并解压，复制 `.env.example` 为 `.env`。
3. 设置 `JWT_SECRET`、`WECHAT_APPID`、`WECHAT_SECRET`，以及 `DEEPSEEK_API_KEY`（可选 `MINIMAX_API_KEY`）。
4. 设置 `HARNESS_ENABLED=true`、`HARNESS_PROVIDER=deepseek-official`、`HARNESS_MODEL=deepseek-v4-flash`，以及官方适配器使用的 `LLM_API_KEY` / `LLM_BASE_URL`。Docker 会安装同版本 SDK 和 runtime wheel。
   默认沙箱模式使用官方 `DSH_PERMISSION_MODE=workspace-write`；审批与工具策略由 Harness 自己处理。
5. 执行 `docker compose up -d --build`，反向代理将 `https://api.example.com` 转到 `127.0.0.1:8765`；详细步骤见独立后端的 README。
6. 用 `GET /health` 检查服务；把小程序 `app.ts` 的 `apiBase` 改为 HTTPS API 地址后，在开发者工具「详情 → 域名信息」刷新配置。

服务默认使用 SQLite 与本地 `uploads/`，适合单机部署；生产扩容时把数据库替换为 PostgreSQL，把上传目录替换为 MinIO/S3，并把文档解析任务移到队列。文件解析已支持 PDF、DOCX、TXT、Markdown、CSV 和图片 OCR。Docker 部署会安装 Tesseract 中文/英文语言包；非 Docker 部署请确保系统已安装 `tesseract-ocr` 与 `chi_sim` 语言数据。

## AI 对话实现

对话采用 SSE 增量事件：先发送 `meta`（conversation_id 与 sources），再发送多个 `delta`，最后发送 `done`。对话与任务统一调用官方 `deepseek-harness-sdk`，由 bundled `dsh --profile sdk` 负责 Agent loop、会话持久化、compaction、重试、工具、技能和权限判定；后端只负责知识检索、用户隔离、消息落库、SSE 协议，以及把官方 `session.event` 转成小程序事件。
文件产物只认官方 `dsh-tool-present` 的 `deliverables/presented` 事件，不再扫描工作区猜测模型生成了什么。`HARNESS_ENABLED=false` 或缺少模型凭据时，聊天接口明确返回配置错误，不回落直连模型；模型密钥始终只保留在服务端。

未配置 `HARNESS_ENABLED=true` 或缺少模型凭据时，聊天接口返回明确的配置错误；模型密钥始终只保留在服务端。

## 上线前清单

法律与合规文案（关于 cola 知识库、数据管理、隐私安全、小程序隐私保护指引、AI 隐私政策、用户服务协议、软件许可及服务协议、会员服务条款）统一放在后端 `server/content/legal/`，由 `scripts/import_content.py` 导入数据库、经 `/api/content/legal` 下发到小程序。修改文案不需要重新提交小程序审核；改完之后重跑一次导入即可。清单与占位符说明见 `server/content/legal/README.md`。

- 补齐运营者信息：在 `.env` 里设置 `LEGAL_OPERATOR_NAME`（真实运营者名称）、`LEGAL_CONTACT_EMAIL`、`LEGAL_ICP_NUMBER`，然后重跑导入；漏填可以用 `python scripts/import_content.py --check` 查出来。
- 完成微信小程序主体认证，并在微信后台把《用户隐私保护指引》按 `server/content/legal/README.md` 的清单逐项勾选；开启客服，保证注销与退款有入口。
- 配置真实 `WECHAT_APPID/SECRET`，不要使用开发回退登录。
- 使用 HTTPS 域名、生产 `JWT_SECRET`，并恢复 `urlCheck: true`。
- 配置 DeepSeek/MiniMax Key，设置反向代理超时和上传大小限制。
- 核对虚拟支付道具价格与 `PLAN_CATALOG`、以及 `content/legal/entries/07-plan.md` 三处一致。
- 将 `data/`、`uploads/` 纳入备份，配置日志与异常告警。
