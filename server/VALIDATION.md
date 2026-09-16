# 验证记录

日期：2026-09-13（Mac）。后端独立路径：`/Users/mac/WeChatProjects/zhi-reader-api`。

## 已完成

| 检查 | 结果 |
| --- | --- |
| 独立 Python 虚拟环境安装 | Python 3.13.11，全部依赖安装完成，pip check 通过 |
| 从 `/tmp` 使用绝对路径启动 | 通过，配置和数据库路径不依赖 shell 工作目录 |
| 后台启动、状态查询、停止、重启 | 通过，端口 8765 |
| GET /health | 200，服务名“知库资料服务” |
| GET /ready | 200，数据库 ready |
| OpenAPI 文档 | 可访问 /docs 和 /openapi.json |
| TypeScript 诊断 | 微信开发者工具自带编译器诊断 0 项（包括本轮 API 地址与过期 token 处理修改） |
| Docker Compose 配置解析 | docker compose config --quiet 通过 |
| Docker 镜像构建 | zhi-reader-api:verify 成功；本次平台 linux/arm64，Python 3.12 |
| 容器内回归 | 同一套 8 项测试全部通过 |
| 容器运行 | UID 10001 非 root；/ready 可用；健康检查已配置 |
| OCR 运行依赖 | 容器内 chi_sim、eng 语言包存在 |
| Docker 数据持久化 | 删除并重建验证容器后，数据库卷和上传卷的测试文件均保留 |
| 独立源码包 | 解压到临时目录后 8 项测试通过；包内不含密钥、证书、业务数据或前端 |
| 小程序过期 token | 使用模拟微信请求验证：旧 token 及延迟并发 401 只触发一次登录，并保留新 token |

本次容器构建通过 `DEBIAN_MIRROR=https://mirrors.aliyun.com` 和 `PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple` 完成。官方软件源在本机 Docker 网络中下载缓慢；默认配置仍保留官方 HTTPS 地址。镜像 SHA256：`415c2c73ec2446941668e84e058028b86996b703f590c79354b4a2f9fab5fcef`。未验证 linux/amd64 构建。

验证容器和专用临时数据卷已清理。Mac 原生后台服务保留运行，端口 8765；日志见 `logs/api.log`，可用 `scripts/manage.py status` 查询。

## 自动 HTTP 与配置回归

执行 `.venv/bin/python -m unittest discover -s tests -v`，8 项通过：

1. 相对数据库、上传和证书路径与 shell 工作目录无关。
2. 默认弱密钥、未配置微信登录的生产环境、不支持的数据库协议被拒绝。
3. 健康检查、有效/无效认证、未配置微信登录/支付、无签名支付回调。
4. 中文文档上传、检索、包含引号/连字符的查询、下载、重试、删除后的列表与配额统计。
5. DOCX 内容提取、损坏 PDF 解析失败、失败重试状态、不支持文件格式。
6. SSE meta/delta/done、来源文档关联、对话持久化、删除资料库后的对话/索引/文件清理。
7. 跨用户资料库、文档、下载和对话写入的隔离。
8. 账号注销后的索引/文件清理及旧 token 失效。

测试启动独立 HTTP 服务并使用临时数据库；测试代码仅在该临时数据库创建用户和 JWT。没有往运行中数据库注入测试 token，也没有新增测试登录入口。测试不产生真实支付、不调用真实模型。

## 本轮验证（2026-09-16，Mac，端口 8765 / HTTPS 9443）

| 检查 | 结果 |
| --- | --- |
| 技巧内容源文件导入数据库 | `scripts/import_content.py` 首次导入 8 篇正文、16 张配图（版本 2026.09.16.4）；再次执行提示「源文件未变化，跳过导入」 |
| GET /api/content/tips | 200，返回 3 个分组、8 篇条目，封面为 `/api/content/assets/*` 逻辑地址 |
| GET /api/content/tips/skill | 200，14 个 blocks（heading/paragraph/step/bullet/note/figure），figure 指向 `fig-skillpick.png` |
| GET /api/content/assets/cover-skill.png | 200，`image/png`，44485 字节，ETag 存在；不存在的文件与 `..%2F..%2F.env` 均为 404 |
| HTTPS 9443 同上两接口 | 200，小程序开发者工具可直接取图 |
| 技能选中 → 注入 | 服务端偏好里只有用户选中的 2 个技能（`builtin-organize-knowledge`、`builtin-write-report`），后端日志 `injected=['organize-knowledge', 'write-report'] loaded=['organize-knowledge', 'write-report']` |
| 技能节点不再出现在对话 | 同一轮 SSE 过程节点只剩 `step` / `reason`（原先会多出「技能目录：4 项可用」与两条「加载技能 · X」），回答正常 `done` |
| 一轮对话未被破坏 | 修复过程中出现的 `name 'state' is not defined`（技能日志误放在 `stream_answer`）已消除，`finish=completed` |


## 多租户隔离 / 权限边界 / 计划模式（2026-09-16 补测）

这一轮把「运行时怎么被隔离、越权会不会发生、计划模式怎么回到小程序」逐个实测了一遍。
证据来自现场运行的服务、以及真机模拟器里的实际点按。

### 1. 多租户隔离：工作区与 DSH_HOME 按租户拆开

运行时进程的 `DSH_HOME` 与进程工作目录在启动时固化，所以隔离边界就是「每个租户一组运行时」：

| 资源 | 位置 | 隔离性 |
| --- | --- | --- |
| 工作区（agent 的 cwd、`DSH_WORKSPACE_ROOT`） | `<HARNESS_WORKSPACES>/users/<租户>/` | 租户私有 |
| 会话 / 技能 / 存储 / 附件 | `<HARNESS_HOME>/users/<租户>/` | 租户私有 |
| 计划评审队列 | `<HARNESS_HOME>/users/<租户>/plan-bridge/` | 租户私有 |
| `profiles/`（profile 组合与插件解析根） | `<HARNESS_HOME>/profiles/` | 部署级只读，软链共享，不复制 node_modules |

租户目录名由 `_tenant_id()` 收敛（剔除 `/`、`.` 等字符，空值归到 `anonymous`），因此 `user_id` 无法借路径穿越跳出根目录。运行时按 `租户 + profile + 模型 + 推理强度` 池化：冷启动实测约 1s，因此池化是划算的；池有上限与空闲回收，正在跑轮次的不回收。

实测：

| 检查 | 结果 |
| --- | --- |
| 探针：新租户 DSH_HOME + 软链 profiles | initialize 成功，启动 1.1s，会话落在 `users/probeuser/sessions/...` |
| 真实问答写入位置 | 产物只出现在 `harness-workspaces/users/eb48428d.../`，没有写进别的租户目录 |
| `tests/test_harness_isolation.py` | 路径穿越收敛、目录互不相同、技能根隔离、跨租户评审不可见，全部通过 |

**已知缺口（必须在正式上线前处理）**：`workspace-write` 只约束「写」。实测沙箱内的 agent 仍然可以**读**工作区之外的文件（探针让它读 `/etc/hosts` 与后端源码目录，都成功）。也就是说，写越权不会发生，但读越权会发生——共享 SQLite 数据库与 `.env` 都在同一操作系统用户下，属于可读范围。

产品侧建议按优先级处理：把 harness 运行时跑在**独立操作系统用户/独立容器**里，并只给它读租户工作区与只读技能目录的权限（这是唯一能真正切断读越权的做法）。当前版本已把 agent 的可见工作区收敛到租户目录，但它不能替代操作系统级隔离。

### 2. 权限预设：workspace-write + never（fail closed）

部署决定写在 `server/harness_runtime/profiles/sdk/cordis.patch.yml`，后端启动时同步到 `$DSH_HOME/profiles/<profile>/`，是唯一落点：

- `sandbox-policy`：`mode: workspace-write`，`workspaceRoot` 取自 `DSH_WORKSPACE_ROOT`（每租户注入）。
- `approval`：`policy: never`。默认在 workspace-write 下是 `ask`，而官方 SDK 传输层不转发 `approval/request`，`ask` 会让工具永久挂起；固定 `never` 后越权动作被确定性拒绝。
- `permission`：官方预设表里没有 `(workspace-write, never)` 组合，组合必须能落到某个预设上，否则运行时直接拒绝启动（实测报错 `composed sandbox and approval defaults match no preset`）。因此补了一条同语义预设 `workspace-write-no-prompt` 并设为默认。

### 3. 计划模式：计划先评审、批准后再执行

官方 SDK 传输层只转发 `session.event` / `session.status`，不含提问/审批通道，`/plan` 命令也不可解析（命令执行入口只由交互式 UI 载具调用）。因此用官方留的扩展点补上：

- `server/harness_runtime/plan_bridge/`：`@cola/dsh-plan-bridge` 插件，注册 `user-questions/request` waterfall（计划评审）与 `agent/pre-step`（计划模式开关）。
- 后端 `set_plan_mode()` 写 `<租户>/plan-bridge/<会话>.mode.json`，插件在步骤边界消费并调用官方 `ctx.planMode.set()`；`exit_plan_mode` 提交计划后，插件写 `<会话>.review.json` 并阻塞等待 `<会话>.answer.json`。
- 计划正文经既有 `session.event` 通道回到后端 → 变成 `kind='plan'` 的过程节点 → 小程序渲染成「页面附着」卡片。
- `POST /api/chat/plan-review` 回写结论。评审超时或不存在时返回 `accepted=false`，前端提示重新提问，**绝不假装已批准**；插件侧超时按官方契约抛 `ASK_CANCELLED`，留在计划模式。

实测（真机模拟器 + 现场服务）：

| 检查 | 结果 |
| --- | --- |
| 计划卡片出现 | `hasPlan: true, planState: review`，计划 markdown 2459 字，渲染 HTML 4548 字，标题取计划自身的一级标题 |
| 就地批准 | 卡片上点「批准并执行」→ `POST /api/chat/plan-review` 返回 `accepted: true` → 卡片转为 `approved`，运行时退出计划模式并在同一轮继续执行 |
| 复盘日志 | 同轮 SSE 出现 `[plan] 计划已批准，开始执行`，随后是执行步骤；`finish=completed` |
| 计划随对话落库 | 计划节点与过程节点一起写进 `messages.trace_json`，重进对话时由 `hydrateAssistant` 复原（默认收起） |

### 4. 深度思考开关真正生效

原来「快速 / 深度」返回同一个 profile，开关是失效的。现在 `thinking_config()` 按开关给出 `(profile, model, reasoning_effort)`：快速 `HARNESS_QUICK_REASONING_EFFORT`（默认 low），深度 `HARNESS_DEEP_REASONING_EFFORT`（默认 high），并可用 `HARNESS_DEEP_MODEL` 换模型。推理强度在运行时 `initialize` 时固化，所以两种档位各持一组运行时；后端日志已按档位区分（`effort=low` / `effort=high`）。

### 5. 会话复用（上下文不再膨胀、重进对话保留过程）

原来每轮都用 `conversation_id + 随机后缀` 新建 harness 会话，上下文靠把整段历史重新拼进提示词维持。现在会话 id 与后端对话 id 绑定并跨轮复用，上下文由运行时自己的事件日志承载、压缩交给官方 compaction；只有没有可复用会话时（预热）才回退到内联历史。过程与思考随消息落库，重进对话可复原。

## 合规文档与白屏修复（2026-09-16 晚，Mac，端口 8765）

本小节记录「关于 cola 知识库 / 数据管理 / 隐私安全 / 小程序隐私保护指引 / 用户服务协议 / 软件许可及服务协议 / 会员服务条款」七篇合规文案的上线准备，以及修复过程中发现并解决的一个会直接导致小程序白屏的样式编译错误。

### 白屏根因：WXSS 不支持 `> :first-child`

模拟器一度整页白屏，逻辑层 `getCurrentPages()` 为空、渲染层 body 仅 820 字节，控制台只有基础库启动日志。用开发者工具自带的 WXSS 编译器（`/Applications/wechatwebdevtools.app/Contents/Resources/app.asar.unpacked/node_modules/wcc-exec/wcsc`）逐个文件编译后定位到：

```
./pages/legal/index.wxss(26:15): error at token `:`
```

出错行是 `.legal-body > :first-child { margin-top: 0; }`。WXSS 编译器不接受这种选择器，编译失败会直接停止渲染，而页面上看不到任何报错。改成由数据侧标记首块（`withKeys` 写入 `first`）、样式用 `.legal-first` 后，整页恢复正常。

结论：`wcsc` / `wcc` 可以直接在命令行复现开发者工具的编译结果，比在模拟器里看白屏快得多，已纳入日常自检。

### 后端接口

| 检查 | 结果 |
| --- | --- |
| `GET /health` | 200，`harness.enabled=true`，provider `deepseek-official`，model `deepseek-flash` |
| `GET /api/content/legal` | 200，7 篇，版本 `2026.09.16.1` |
| `GET /api/content/legal/{id}` | 7 个 id 全部 200，无 `{{` 占位符残留 |
| `scripts/import_content.py --slug legal --check` | 打印库内现状与 3 项待补齐的运营者信息 |
| 文案指纹幂等 | 源文件未变化时跳过导入；改身份变量会触发重导（`tests/test_legal_content.py`） |

### 小程序实际渲染

在开发者工具里逐个打开七个页面，读取真实 DOM（`*.legal-h2` / `.legal-line` / `.legal-note` 等）：

| 页面 | 渲染结果 |
| --- | --- |
| about | 6 个小标题、10 条列表、客服按钮、6 条相关说明 |
| data | 7 个小标题、28 条列表，额外渲染 2 个动作按钮（复制资料库清单 / 清理本地缓存）与客服按钮 |
| privacy | 7 个小标题、23 条列表、1 个提示块 |
| guide | 10 个小标题、8 段正文、28 条列表、5 个提示块 |
| terms | 13 个小标题、51 条列表 |
| license | 9 个小标题、27 条列表 |
| plan | 8 个小标题、21 条列表 |

七页共同检查项：`placeholder=false`（无 `{{` 泄漏）、`badWords=false`（不出现 LLM wiki / knowledge / ima）、`overflowX=0`（无横向溢出）、首块上边距为 0、`open-type="contact"` 客服入口存在、页脚与更新日期/生效日期正常。

### 未完成（需要用户在微信后台配合）

`LEGAL_OPERATOR_NAME`、`LEGAL_CONTACT_EMAIL`、`LEGAL_ICP_NUMBER` 三项尚未填写，当前文案里的运营者显示为通用表述；微信后台《用户隐私保护指引》的勾选项、客服开通情况也需要在提交审核前逐项核对。清单见 `content/legal/README.md`。

## 验证边界

真实微信登录、支付下单/回调/会员权益、真实模型回答仍需正式参数联调。SSE 测试使用“模型尚未配置”的提示，不代表模型能力已验收。未进行微信真机 UI、负载和公开上线审核验收。本记录只证明后端分离、本地运行和所列接口测试结果。
