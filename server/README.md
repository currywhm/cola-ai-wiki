# 知库独立后端

本目录可以单独复制到 Mac 或 Linux 服务器运行，不依赖微信小程序源码、Node.js 或微信开发者工具。API 为 FastAPI，数据库为 SQLite，上传文件保存在本机持久目录。

## Mac 启动

需要 Python 3.11+。图片 OCR 还需要 Tesseract 中文语言包（Homebrew：`brew install tesseract tesseract-lang`）。

```bash
cd /Users/mac/WeChatProjects/zhi-reader-api
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
- `DATABASE_URL` 默认 `sqlite+aiosqlite:///./data/llmwiki.db`；当前实现仅支持 SQLite，其他数据库协议会明确拒绝。
- `UPLOAD_DIR` 默认 `./uploads`。数据库、上传目录、支付证书的相对路径均以本后端目录为基准。
- 微信登录需要 `WECHAT_APPID`、`WECHAT_SECRET`，没有测试账号回退接口。
- 当前支付模式为个人主体微信虚拟支付：需要 OfferID、现网 AppKey、道具 ID、道具价格和公网 HTTPS 发货推送地址。AppKey 只放在后端 `.env`，小程序端只接收服务端签名后的 `payData`。配置项见 `.env.example`。
- 上传文档后会自动生成标题、摘要、标签和关键要点，并持久化到文档记录；已配置模型时使用两步整理提示，未配置模型时使用可追溯的本地基础整理。问答会优先参考整理结果，再引用原文片段。
- 模型配置见 `DEEPSEEK_*`、`OPENAI_*`、`MINIMAX_*`。未启用 Harness 时，三者使用 OpenAI Chat Completions 兼容协议；未配置模型时仍可完成本地基础整理，但问答只返回配置提示。
- Harness 运行时：后端安装匹配版本的 `deepseek-harness-sdk` 与 runtime 后，设置 `HARNESS_ENABLED=true`、`HARNESS_HOME`、`HARNESS_PROFILE=sdk`、`HARNESS_PROVIDER=deepseek-official`、`HARNESS_MODEL=deepseek-v4-flash`、`HARNESS_RUNTIME_MODE`。每次问答会创建只含 `knowledge-context.md` 的临时服务端工作区，Harness Agent/Skill 仅可读取该资料上下文；小程序端不接收 Harness 配置、插件或密钥。`HARNESS_STRICT=true` 时问答链路只走 Harness，runtime、profile、provider、model 或后端模型密钥缺失都会返回明确错误，不再静默回退到旧 OpenAI-compatible 链路。
- “问全网”同样只在后端执行：设置 `WEB_SEARCH_PROVIDER=tavily` 或 `brave`、`WEB_SEARCH_API_KEY` 和对应的 `WEB_SEARCH_BASE_URL` 后，服务端先取得公开网页结果，再将带来源的检索上下文交给 Harness。小程序端只传 `mode=web`，不会引入 Harness、搜索 SDK 或任何密钥。未配置搜索凭据时接口会明确返回配置错误，不会把普通知识库回答冒充为全网结果。
- 运营内容（使用技巧）源文件在 `content/tips/`，运行时从数据库读，部署后执行 `python scripts/import_content.py` 导入，详见下文「运营内容」一节。
- 技能不进对话展示：用户在小程序里选中的技能由后端解析并注入（内置技能包走 Harness `skill` 工具、我的技能走技能指令），选中状态只体现在输入框技能图标变蓝；接口不返回技能过程节点。
- `APP_ENV=production` 启动时检查微信登录参数；所有环境均拒绝默认或过短的 JWT 密钥。

原小程序的 `server` 现在仅为指向本目录的兼容符号链接；后续后端改动以本目录为唯一代码源。现有数据库和上传文件随目录移动保留。新生成的 JWT 密钥会使旧开发 token 失效，需要重新微信登录。

## 运营内容（使用技巧）

「使用技巧」这类图文内容的源文件放在 `content/tips/`：`manifest.json` 是清单（顺序、标题、摘要、封面、正文文件、阅读时长），`entries/*.md` 是正文，`assets/*.png` 是配图。**运行时统一从数据库读取**（表 `content_meta` / `content_entries` / `content_assets`），容器里没有 `content` 目录也能正常展示，改文案也不用重新发小程序。

部署或更新内容时执行一次：

```bash
.venv/bin/python scripts/import_content.py            # 指纹没变自动跳过，可反复执行
.venv/bin/python scripts/import_content.py --force    # 强制重新导入
.venv/bin/python scripts/import_content.py --check    # 只查看库里当前有什么
```

不执行这一步也能用：服务启动时若发现库里没有内容、而 `content/tips` 目录存在，会自动导入一次。正文的 Markdown 在导入时就解析成结构化 blocks 存库，请求时不再解析；`manifest.json` 的 `version` 用于小程序端缓存失效。配图接口 `/api/content/assets/{文件名}` 不挂登录态（小程序 `<image>` 无法携带 token），只允许读取该目录内的单个文件名，返回二进制并带 ETag。

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

Harness 部署要求 runtime 与服务器平台一致。本机源码构建联调可以使用 `HARNESS_RUNTIME_MODE=node`，依赖系统 Node.js 和源码目录中的 node carrier；Zeabur/Linux 线上部署应安装或构建 Linux x64/arm64 的 `deepseek-harness-runtime-bin`，并使用 `HARNESS_RUNTIME_MODE=exe`。不要把 macOS runtime 复制到 Linux 服务器。启用 `HARNESS_ENABLED=true` 前，必须在后端环境配置真实 `DEEPSEEK_API_KEY`，否则问答接口会返回 Harness 配置错误。

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

本产品的模型侧只有这些能力：**对话、知识库检索问答、读写文档、整理文档、联网检索、技能、子智能体**。
它**没有**任何命令、脚本或可执行文件的执行能力，也无法读写当前用户工作区以外的服务器文件。
这条边界由两层而非一句提示词来保证：

1. **运行时插件行关闭**（`harness_runtime/profiles/sdk/cordis.patch.yml`）：
   `tool-bash` / `tool-pwsh` / `tool-jobs` / `tool-workflow` / `workflow-worker-thread` /
   `tool-ralph` / `tool-fs-search`（会 spawn `rg`）全部 `disabled: true`。
   这些工具不会出现在模型可见的工具清单里（实测清单：`read` / `read_image` / `write` / `edit` /
   `skill` / `web_search` / `web_fetch` / `subagent*` / `todo_write` / `goal` /
   `exit_plan_mode`），调用不会有任何东西响应。
   注意：`bash-sandbox` / `shell-env` 这类**服务行必须留着**——它们只提供 `ctx.shell`/`ctx.shellEnv`
   内部服务，权限预设等行在等它们；关掉会让整棵插件树加载失败（表现为问答永久挂起）。
2. **工具层守卫插件** `@cola/dsh-safety-guard`（`harness_runtime/safety_guard/`）：
   用官方 `ctx.tools.guard()` 做单调否决：执行类工具名一律拒绝；`read`/`write`/`edit`/`read_image`
   等工具的参数路径只要落在租户工作区之外就拒绝。这一层不依赖模型是否听话——文档正文、技能指令、
   用户输入里的提示注入都被这层拦住（官方沙箱只限制写、不限制读，所以读的收口必须在这里做）。

每个租户的工作区是 `harness-workspaces/users/<用户 id>`，`DSH_HOME` 是 `harness-home/users/<用户 id>`，
技能、会话、附件、工作区全部按租户分目录，互不可见；上传白名单只收文档与图片（不含脚本、可执行文件）。
改动上述任一层后，务必用「要求执行命令 / 要求读取 `.env` / 恶意技能」三类请求回归一次：
正确表现是**零工具调用 + 明确说明能力边界**，且会话日志里的工具 schema 清单不含执行类工具。

## 服务边界

小程序端只承担微信登录、文件选择与上传、页面展示和 SSE 对话呈现。`llm_wiki` 风格的文档整理（标题、摘要、标签、要点、持续的知识条目）以及 Harness 参考的会话流式事件都运行在本后端；小程序不包含 Node.js、Harness 运行时、整理提示词或任何模型密钥。启用 Harness 后，问答由后端 Harness profile/provider/model 执行；未启用 Harness 时，后端才通过 OpenAI-compatible 接口访问 DeepSeek、OpenAI 或 MiniMax。

目前可以独立部署和完成本地接口联调。上线前仍需真实微信登录、支付回调与会员权益发放、模型输出质量、并发及上传安全验收。当前解析在请求内完成，长文档/OCR 应迁移到任务队列；检索为全文检索，展示分数不是经过校准的语义相似度。可运行不等于已通过微信上线审核。
