# 知库独立后端

本目录可以单独复制到 Mac 或 Linux 服务器运行，不依赖微信小程序源码、Node.js 或微信开发者工具。API 为 FastAPI，本地默认使用 SQLite + 本地上传目录，微信云托管可切换为 MySQL + COS。

## Mac 启动

需要 Python 3.11+。图片 OCR 还需要 Tesseract 中文语言包（Homebrew：`brew install tesseract tesseract-lang`）。

```bash
cd /Users/mac/WeChatProjects/wechatllmwiki/server
python3 scripts/manage.py setup
.venv/bin/python scripts/manage.py start
.venv/bin/python scripts/manage.py status
curl http://127.0.0.1:8765/ready
```

`setup` 创建专用 `.venv` 和权限为 600 的 `.env`，首次生成随机 JWT 密钥；不会覆盖已有 `.env`。`start` 默认后台运行并记录 PID，端口占用时直接报告。停止使用 `.venv/bin/python scripts/manage.py stop`，前台运行使用 `run`。支持 `--port 8766` 指定端口。后台服务持续到手动停止或 Mac 重启，未安装开机任务。

如果本机已有与当前 CPU 架构不匹配的旧 `.venv`，管理脚本会自动使用同目录下的 `.venv-x86`（由维护者用于本机联调），服务器部署仍使用标准 `.venv`。

健康检查 `/health`；数据库就绪检查 `/ready`；交互式接口文档 `/docs`；API 规范 `/openapi.json`。日志位于 `logs/api.log`。可从任意工作目录通过脚本绝对路径启动。

## 配置和数据

- `.env` 以本后端目录为基准加载，进程环境变量优先。
- 本地 `DATABASE_URL` 默认 `sqlite+aiosqlite:///./data/llmwiki.db`。微信云托管 MySQL 模板注入 `MYSQL_ADDRESS` / `MYSQL_USERNAME` / `MYSQL_PASSWORD` / `MYSQL_DATABASE`，后端会自动拼连接串并建表；显式设置 `DATABASE_URL` 时以后者为准。
- `STORAGE_BACKEND=local|cos`。`local` 使用 `UPLOAD_DIR`；`cos` 时文件写入 `Bucket` / `Region`（兼容 `COS_BUCKET` / `COS_REGION`）和可选 `COS_PREFIX`，数据库里的 `storage_path` 保存 `cos://...` 引用。云托管模式不要配置长期 `COS_SECRET_ID` / `COS_SECRET_KEY`，后端会调用开放接口 `/_/cos/getauth` 获取临时密钥。
- `UPLOAD_DIR` 默认 `./uploads`。数据库、上传目录、支付证书的相对路径均以本后端目录为基准。
- 微信登录需要 `WECHAT_APPID`、`WECHAT_SECRET`，没有测试账号回退接口。
- `WECHAT_APPID` 就是小程序后台“开发者 ID / AppID”，格式为 `wx` 开头 18 位；`WECHAT_SECRET` 为 32 位 AppSecret。`APP_ENV=production` 启动时会调用微信 `stable_token` 实时校验，格式错误或凭证无效都会导致服务启动失败。
- 微信登录、凭证和客服接口默认使用 `https://api.weixin.qq.com`，且不继承云托管环境的 `HTTP(S)_PROXY`，避免出口代理证书被误当成微信官方证书。只有平台明确要求出口代理时才设置 `WECHAT_HTTPS_PROXY`；代理使用私有根证书时用 `WECHAT_CA_FILE` 指向 PEM CA，不能使用微信支付/开放平台 API 证书。
- 微信接口出现临时网络/TLS 故障时，启动校验会记录告警但不会让容器无限重启；微信明确返回凭证错误时仍会拒绝启动。
- 头像：`POST /api/me/avatar`（≤ 2MB，jpg/png/webp）保存用户选定的微信头像。微信官方「头像填写能力」`<button open-type="chooseAvatar">` 回调给的是本机临时路径（`http://tmp/...` / `wxfile://...`），换设备或重装就失效，所以必须落到服务端。每个用户只保留一份（换头像时删旧文件，不留垃圾），`GET /api/avatars/{user_id}` 公开读取——小程序 `<image>` 带不了 Authorization 头，安全性靠文件名只保留 `[0-9A-Za-z_-]`（不可能路径穿越）+ user_id 是随机 32 位十六进制，与 `/api/content/assets` 同一取舍。`PATCH /api/me` 的 `nickname` / `avatar` 均为可选字段，未传即保持原值（只改昵称不会顺手把头像清掉）。
- 当前支付模式为个人主体微信虚拟支付：需要 OfferID、现网 AppKey、道具 ID、道具价格和公网 HTTPS 发货推送地址。AppKey 只放在后端 `.env`，小程序端只接收服务端签名后的 `payData`。配置项见 `.env.example`。
- 登录与隐私（前端）：无令牌时 `ensureAuth()` 只回 `pages/login/index`，不会静默换取登录态。点击“微信登录”先弹底部协议提示，用户点“同意”后才调用 `wx.login`；服务端按 OpenID 自动创建或关联账号，默认昵称“微信用户”、默认头像使用产品图。用户之后可在“我的 → 账号设置”通过 `<button open-type="chooseAvatar">` 和 `<input type="nickname">` 主动选择并保存，登录本身不调用 `wx.getUserInfo` / `wx.getUserProfile` / `<open-data>`。相册、拍照、微信文件等系统隐私能力交给微信官方弹窗处理，`services/privacy.ts` 不注册 `wx.onNeedPrivacyAuthorization`。
- 上传文档后会自动生成标题、摘要、标签和关键要点，并持久化到文档记录；已配置模型时使用两步整理提示，未配置模型时使用可追溯的本地基础整理。问答会优先参考整理结果，再引用原文片段。
- 模型配置见 `DEEPSEEK_*`、`OPENAI_*`、`MINIMAX_*`。未启用 Harness 时，三者使用 OpenAI Chat Completions 兼容协议；未配置模型时仍可完成本地基础整理，但问答只返回配置提示。
- 对话与任务只有一条模型链路：官方 `deepseek-harness-sdk` 的 `DeepSeekHarness.run()`。`LLM_API_KEY` / `LLM_BASE_URL` 是官方适配器的凭据覆盖，`HARNESS_PROVIDER` 和 `HARNESS_MODEL` 决定 Harness 路由；`DEEPSEEK_*` / `OPENAI_*` / `MINIMAX_*` 仅保留给文档整理等非 Agent 后端工具，不能再作为聊天回退。
- Harness 运行时由官方 SDK 自动启动并复用 bundled `dsh --profile sdk` runtime，不要求手工配置 `HARNESS_RUNTIME_MODE`、源码路径或自建插件：`HARNESS_ENABLED=true`、`HARNESS_HOME`、`HARNESS_PROFILE=sdk`、`HARNESS_PROVIDER=deepseek-official`、`HARNESS_MODEL=deepseek-v4-flash` 即可。每个用户使用独立的 `HARNESS_HOME` 与工作区；会话持久化、compaction、重试、工具循环和技能加载都由 Harness 自己负责。
- “问全网”同样由官方 profile 挂载的 `web_search` / `web_fetch` 工具执行；后端只负责把 `mode=web` 解释为用户意图，不另建搜索 Agent，也不把密钥下发到小程序。
- 文件交付只跟随官方 `dsh-tool-present`：patch 插入与 Web `standard/ptc` preset 相同的官方 row，后端接收 `deliverables/presented` 事件，不再扫描工作区猜产物。
- 额度单位是**积分**而不是问答次数：一轮问答按真实 token 用量扣分。真实成本用 deepseek-flash 官方单价算（元/百万 tokens：输入命中缓存 0.04、未命中 2.00、输出 8.00，空闲时段官方半价、代码按请求时刻自动减半），用户价 = 成本 × `CREDIT_MARKUP`（默认 1.5），积分 = 用户价 ÷ `CREDIT_UNIT_YUAN`（默认 0.001 元）四舍五入且至少 1 分。
- 加价倍率恒定 1.5 倍，但**用户可见的积分会随时段浮动**——同一段对话空闲时段约 3 分、高峰时段约 7 分。这是刻意保留的口径（成本完全转嫁、毛利率恒定），对外文案必须写明「积分随官方计价时段浮动」。
- 月度积分额度：免费试用期 600、试用结束后 150、Plus 3000、Pro 15000（`MEMBERSHIP_LIMITS`），按自然月归零、不结转。`/api/me` 返回 `quota.credits_limit/credits_used/credits_left`，`/api/pay/plans` 返回 `monthly_credits`，SSE 的 `done` 事件带本轮 `credits` 与本月 `credits_used/credits_limit`。
- 每轮结算写入 `usage_logs`（user_id / conversation_id / message_id / model / 四类 token / cost_yuan / credits / created_at），本月已用积分就是这张表的合计，任何一分都能回到具体轮次与 token；后端自用任务（如新建技能）不入用户积分。
- 计费实现集中在 `app/services/credits.py`（纯函数，无 IO）。改价或改额度时，必须同步 `content/legal/`、`content/tips/` 里的口径说明并重跑 `python scripts/import_content.py`。
- 运营内容（使用技巧）源文件在 `content/tips/`，运行时从数据库读，部署后执行 `python scripts/import_content.py` 导入，详见下文「运营内容」一节。
- 技能不进对话展示：用户在小程序里选中的技能由后端解析并注入（内置技能包走 Harness `skill` 工具、我的技能走技能指令），选中状态只体现在输入框技能图标变蓝；接口不返回技能过程节点。
- `APP_ENV=production` 启动时检查微信登录参数；所有环境均拒绝默认或过短的 JWT 密钥。

后端源码统一维护在本仓库的 `server/` 目录；现有数据库和上传文件随目录移动保留。新生成的 JWT 密钥会使旧开发 token 失效，需要重新微信登录。

## 运营内容（使用技巧）

「使用技巧」这类图文内容的源文件放在 `content/tips/`：`manifest.json` 是清单（顺序、标题、摘要、封面、正文文件、阅读时长），`entries/*.md` 是正文，`assets/*.png` 是配图。**运行时统一从数据库读取**（表 `content_meta` / `content_entries` / `content_assets`），容器里没有 `content` 目录也能正常展示，改文案也不用重新发小程序。

部署或更新内容时执行一次：

```bash
.venv/bin/python scripts/import_content.py            # 指纹没变自动跳过，可反复执行
.venv/bin/python scripts/import_content.py --force    # 强制重新导入
.venv/bin/python scripts/import_content.py --check    # 只查看库里当前有什么
```

不执行这一步也能用：服务启动时若发现库里没有内容、而 `content/tips` 目录存在，会自动导入一次。正文的 Markdown 在导入时就解析成结构化 blocks 存库，请求时不再解析；`manifest.json` 的 `version` 用于小程序端缓存失效。配图接口 `/api/content/assets/{文件名}` 不挂登录态（小程序 `<image>` 无法携带 token），只允许读取该目录内的单个文件名，返回二进制并带 ETag。

## 微信云托管（MySQL + COS）

仓库根目录的 `Dockerfile` 专门用于云托管：它只复制 `server/` 后端目录，不包含 `miniprogram/`。云托管选择 GitHub 源码部署时，Dockerfile 路径填写 `/Dockerfile`，构建目录使用仓库根目录。

云托管最小变量样例见 [`server/.env.cloud.example`](.env.cloud.example)。根目录 `Dockerfile` 已内置以下固定值，不需要在控制台重复配置：

```text
APP_ENV=production             APP_NAME=cola知识库
PORT=80                        UPLOAD_DIR=/app/uploads
CONTENT_DIR=/app/content       CORS_ORIGINS=*
STORAGE_BACKEND=cos            COS_PREFIX=cola
WECHAT_API_BASE=https://api.weixin.qq.com
WECHAT_OPENAPI_BASE=http://api.weixin.qq.com
LLM_BASE_URL=https://api.deepseek.com  LLM_MODEL=deepseek-chat
HARNESS_ENABLED=true           HARNESS_PROFILE=sdk
HARNESS_PROVIDER=deepseek-official
HARNESS_MODEL=deepseek-v4-flash
DSH_PERMISSION_MODE=workspace-write
HARNESS_HOME=/app/harness-home
HARNESS_WORKSPACES=/app/harness-workspaces
```

1. 在云托管控制台创建并开启 MySQL，数据库字符集使用 `utf8mb4`。平台会注入 `MYSQL_ADDRESS`、`MYSQL_USERNAME`、`MYSQL_PASSWORD`、`MYSQL_DATABASE`；也可以手工填完整 `DATABASE_URL`，它会覆盖这四项。
2. 创建对象存储桶，记下桶名和地域；在云托管服务中开启「开放接口服务」，后端才能无密钥调用 `/_/cos/getauth`。开启后必须重新构建并发布新版本。
3. 在服务环境变量中只配置账号凭据、平台资源和合规信息（MySQL 的四个 `MYSQL_*` 通常已由平台自动注入）：

```env
JWT_SECRET=至少32位随机字符串
WECHAT_APPID=小程序AppID
WECHAT_SECRET=小程序AppSecret
LLM_API_KEY=大模型APIKey
MYSQL_ADDRESS=内网IP:3306
MYSQL_USERNAME=用户名
MYSQL_PASSWORD=密码
MYSQL_DATABASE=数据库名
Bucket=对象存储桶名
Region=ap-shanghai
LEGAL_OPERATOR_NAME=运营者名称
LEGAL_CONTACT_EMAIL=联系邮箱
LEGAL_ICP_NUMBER=ICP备案号
```

`Bucket` / `Region` 是云托管官方对象存储示例的变量名；已有的 `COS_BUCKET` / `COS_REGION` 仍然兼容。容器默认监听端口为 `80`，与微信云托管当前服务的健康检查端口保持一致；如平台明确要求其他端口，可用 `PORT` 覆盖。

4. 确认服务已关闭旧版 SQLite/本地上传卷依赖。新版本首次启动会创建 MySQL 表；`content/tips` 与 `content/legal` 仍会在启动时按指纹导入数据库。已有本地 SQLite 数据不会自动迁移到 MySQL，需要单独做一致性迁移。

MySQL 没有真正使用 FTS5：`chunks_fts` 作为普通 InnoDB 表保存检索元数据，现有检索逻辑按 `chunks.content` 做中文字符串命中评分；COS 文件只在后端解析或下载时临时落到容器磁盘，请求结束后删除。

## 微信客服（联系客服）

小程序侧的入口只用一个原生按钮（`pages/legal`、`pages/mine` 各一个）：

```html
<button open-type="contact" session-from="legal-data" bindcontact="onContact">联系客服</button>
```

点击后由微信拉起**原生客服会话窗口**。这是唯一合规且可用的路径：自研聊天页拿不到客服会话额度、也不能转人工，还多一份聊天记录的合规负担。前端因此**不渲染、不存储**任何客服聊天内容，`bindcontact` 只负责记来源埋点与按需跳页。

开发者要做的是后端。`app/services/wechat_kf.py` 覆盖官方 kf-message 的全部 10 个接口：

| 能力 | 接口 | 后端路由（均需 `X-Kf-Admin-Token`） |
| --- | --- | --- |
| 添加客服账号 | `POST /customservice/kfaccount/add` | `POST /api/wechat/kf/accounts` |
| 删除客服账号 | `POST /customservice/kfaccount/del` | `DELETE /api/wechat/kf/accounts?kf_account=` |
| 获取所有客服账号 | `GET /cgi-bin/customservice/getkflist` | `GET /api/wechat/kf/accounts` |
| 获取在线客服列表 | `GET /cgi-bin/customservice/getonlinekflist` | `GET /api/wechat/kf/accounts/online` |
| 设置客服管理员 | `GET /customservice/kfaccount/setadmin` | `POST /api/wechat/kf/accounts/admin` |
| 取消客服管理员 | `GET /customservice/kfaccount/canceladmin` | `DELETE /api/wechat/kf/accounts/admin?kf_openid=` |
| 发送客服消息 | `POST /cgi-bin/message/custom/send` | `POST /api/wechat/kf/send` |
| 客服输入状态 | `POST /cgi-bin/message/custom/business/typing` | `POST /api/wechat/kf/typing` |
| 上传临时素材 | `POST /cgi-bin/media/upload` | `POST /api/wechat/kf/media` |
| 获取临时素材 | `GET /cgi-bin/media/get` | 入站图片/语音/视频自动落为 `kf/<openid>/...` 存储对象 |

消息推送（收消息）走 MP 后台配置的回调，不向外暴露：`GET /api/wechat/kf/callback` 验签回 `echostr`，`POST /api/wechat/kf/callback` 收消息（明文与安全模式都支持）。收到的消息落 `kf_messages`、会话汇总落 `kf_sessions`（含未读数），未配置人工客服时回一条带冷却的自动回执（同一用户每小时一次，避免刷屏）。

上线前必须在 MP 后台「开发管理 → 消息推送」打开推送，URL 填 `https://<域名>/api/wechat/kf/callback`，Token / EncodingAESKey 填回 `.env` 的 `WECHAT_KF_TOKEN` / `WECHAT_KF_AES_KEY`。**没配推送时用户发出的消息微信不会推给我们，后台会话列表就是空的**——这是配置缺口，不是代码问题。另需设置 `WECHAT_KF_ADMIN_TOKEN`（管理接口的口令，生产环境不设即关闭）。

`access_token` 统一走官方的 `stable_token`（失败回退 `cgi-bin/token`），缓存在 `wx_tokens` 表，多实例不会互相顶掉；所有接口只在服务端调用，小程序端永远拿不到 `access_token` 或 `appsecret`。

## Linux / Docker 部署


```bash
tar -xzf zhi-reader-api.tar.gz
cd zhi-reader-api
cp .env.example .env
# 编辑 .env：填入随机 JWT_SECRET、微信登录、虚拟支付道具、模型配置和 Harness 配置
mkdir -p cert
# 若切换为传统商户支付，再将支付私钥和平台证书放入 cert
docker compose up -d --build
# 把镜像里的 content/ 源文件导入数据库（幂等；漏执行会在首次启动自动导入一次）
docker compose exec api python scripts/import_content.py
curl http://127.0.0.1:8765/ready
```

Harness runtime 无须在部署脚本里单独选择模式或复制二进制。`deepseek-harness-sdk==0.1.5rc1` 会按当前 Python 平台安装匹配的 `deepseek-harness-runtime-bin`，官方 SDK 自动定位并启动它；升级时同时升级这两个官方 wheel，不需要改业务代码或重新实现 Agent。Linux 上不要复制 macOS runtime。启用 `HARNESS_ENABLED=true` 前，必须在后端配置 `LLM_API_KEY`（或 `DEEPSEEK_API_KEY`），并把 `LLM_BASE_URL` 设为官方端点或兼容网关地址。
Compose 强制生产环境，默认只绑定服务器回环地址 8765；由 Nginx 对外提供 HTTPS。模板见 `deploy/nginx.conf.example`，替换域名及证书路径后使用。已关闭代理响应缓冲以支持 SSE。容器使用非 root 用户，包含中文 OCR、健康检查、重启策略和日志大小限制。

Debian 和 Python 软件源默认使用官方 HTTPS 地址。网络较慢时可在 `.env` 增加 `DEBIAN_MIRROR=https://mirrors.aliyun.com`、`PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple` 后重新构建，仍使用 Debian 签名校验软件包。系统依赖与 Python 依赖分层缓存。

数据库及文件分别使用 Docker 命名卷 `api_data` / `api_uploads`，首次启动为新数据库，不自动导入 Mac 数据。迁移已有数据时先停止写入，再一致地备份数据库与上传文件；已有绝对 `storage_path` 需要按目标上传目录迁移。备份与恢复需保留原 JWT 密钥。不要用 `docker compose down -v` 删除业务数据卷。

小程序开发地址为 `http://127.0.0.1:8765`；真机/上线要使用部署的 HTTPS 域名，在微信平台登记 request、uploadFile、downloadFile 服务器域名，并恢复开发者工具 `urlCheck: true`。真实设备上的 127.0.0.1 指设备自身。

## 验证与打包

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/package.py
```

需要复验容器时：

```bash
docker build -t zhi-reader-api:verify .
.venv/bin/python scripts/verify_container.py
```

容器验证会运行同一套 HTTP 测试，检查非 root 用户、中文/英文 OCR 语言包，并通过删除和重建测试容器验证数据库卷与上传卷保留数据。测试容器和专用卷会自动清理。

测试会在系统临时目录建立专用数据库和文件目录，启动独立 HTTP 进程；测试用户与 JWT 仅存在于该测试环境，不向运行中数据库写入数据。测试结束后停止进程并清理临时数据。

源码包输出 `dist/zhi-reader-api.tar.gz`，使用文件白名单，排除 `.env`、证书、数据库、用户上传文件、虚拟环境和日志。Mac 验证结果与尚未验收项见 `VALIDATION.md`。

## 能力边界与安全（执行能力归零）

## 能力边界与安全（官方 Harness 负责）

后端不维护自建 Agent、计划桥、安全守卫或工作区产物扫描器。运行时组合、工具清单、会话持久化、compaction、重试、权限判定和文件交付都来自官方 `sdk` profile；`harness_runtime/cordis.patch.yml` 只覆盖官方 `system-prompt` row，并插入 Web 同款官方 `@deepseek-ai/dsh-tool-present` row。

默认通过官方 `DSH_PERMISSION_MODE=workspace-write` 运行。官方 profile 会继续自行处理 sandbox、approval 和工具策略；公开 Python SDK 没有审批应答接口，因此危险操作在无人审批时会 fail closed，而不是由后端伪造一个放行策略。生产环境仍应使用最小权限用户或独立容器，并且不要在容器内放置可被读取的密钥文件。

每个租户的工作区是 `harness-workspaces/users/<用户 id>`，`DSH_HOME` 是 `harness-home/users/<用户 id>`；技能、会话、附件、profile 状态与工作区按租户分开，不共享可写 profile。升级官方 SDK/runtime 时，只更新官方 wheel 和必要的官方 row override，不在应用层复制 Agent 逻辑。

## 服务边界

小程序端只承担微信登录、文件选择与上传、页面展示和 SSE 对话呈现。`llm_wiki` 风格的文档整理（标题、摘要、标签、要点、持续的知识条目）是后端非 Agent 工具；问答、技能和任务执行全部由后端官方 Harness `sdk` profile 完成。小程序不包含 Node.js、Harness 运行时、整理提示词或任何模型密钥。`HARNESS_ENABLED=false` 时聊天接口明确返回 `503`，不会回落到直连 Chat Completions。
目前可以独立部署和完成本地接口联调。上线前仍需真实微信登录、支付回调与会员权益发放、模型输出质量、并发及上传安全验收。当前解析在请求内完成，长文档/OCR 应迁移到任务队列；检索为全文检索，展示分数不是经过校准的语义相似度。可运行不等于已通过微信上线审核。
