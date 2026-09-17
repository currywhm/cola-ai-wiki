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

本节已按 2026-09-17 的官方 SDK 方案更新。旧的 `@cola/dsh-plan-bridge`、`@cola/dsh-safety-guard` 和自建记忆链路已经删除，以下内容描述当前代码。

### 1. 多租户隔离：工作区与 DSH_HOME 按租户拆开

运行时进程的 `DSH_HOME` 与进程工作目录在启动时固化，所以隔离边界就是「每个租户一组运行时」：

| 资源 | 位置 | 隔离性 |
| --- | --- | --- |
| 工作区（agent 的 cwd） | `<HARNESS_WORKSPACES>/users/<租户>/` | 租户私有 |
| 会话 / 技能 / 存储 / 附件 | `<HARNESS_HOME>/users/<租户>/` | 租户私有 |
| profile 状态（组合与插件解析根） | `<HARNESS_HOME>/users/<租户>/profiles/` | 租户私有，不共享可写 profile |

租户目录名由 `_tenant_id()` 收敛（剔除 `/`、`.` 等字符，空值归到 `anonymous`），因此 `user_id` 无法借路径穿越跳出根目录。运行时按 `租户 + profile + 模型 + 推理强度` 池化：冷启动实测约 1s，因此池化是划算的；池有上限与空闲回收，正在跑轮次的不回收。

实测：

| 检查 | 结果 |
| --- | --- |
| 探针：新租户 DSH_HOME | 官方 runtime 自行初始化 profile，会话落在 `users/probeuser/sessions/...` |
| 真实问答写入位置 | 产物只出现在 `harness-workspaces/users/eb48428d.../`，没有写进别的租户目录 |
| `tests/test_harness_isolation.py` | 路径穿越收敛、目录互不相同、技能根隔离、官方 patch 校验通过 |

**已知缺口（必须在正式上线前处理）**：`workspace-write` 只约束「写」。实测沙箱内的 agent 仍然可以**读**工作区之外的文件（探针让它读 `/etc/hosts` 与后端源码目录，都成功）。也就是说，写越权不会发生，但读越权会发生——共享 SQLite 数据库与 `.env` 都在同一操作系统用户下，属于可读范围。

产品侧建议按优先级处理：把 harness 运行时跑在**独立操作系统用户/独立容器**里，并只给它读租户工作区与只读技能目录的权限（这是唯一能真正切断读越权的做法）。当前版本已把 agent 的可见工作区收敛到租户目录，但它不能替代操作系统级隔离。

### 2. 权限模式：官方 workspace-write（无审批应答时 fail closed）

部署决定只保留官方 row override，位于 `server/harness_runtime/cordis.patch.yml`，通过官方 `patches=(...)` 参数传入 `DeepSeekHarness`：

- `system-prompt` 使用 `DSH_SYSTEM_PROMPT` 设置 cola 的部署 persona。
- `DSH_PERMISSION_MODE=workspace-write` 直接使用官方 sandbox、approval 与工具策略，不再重写 permission preset。
- 插入 Web `standard/ptc` preset 同款的官方 `@deepseek-ai/dsh-tool-present` row，用于接收 `deliverables/presented` 交付事件。
- 公开 Python SDK 不提供审批应答接口；需要审批的操作在无人应答时由官方 Harness fail closed，后端不伪造放行。

### 3. 计划模式：不伪造官方未提供的评审 RPC

官方公开 Python SDK 没有 `/plan` 传输方法和计划评审 RPC。当前实现不伪造该能力：`payload.plan=true` 返回 `400`，`POST /api/chat/plan-review` 返回 `410`。官方 `sdk` profile 内的 plan-mode 工具仍可产生计划事件，后续若要接回评审，应等官方 SDK 暴露协议，不能在应用层另写 bridge。

### 4. 深度思考开关真正生效

原来「快速 / 深度」返回同一个 profile，开关是失效的。现在 `thinking_config()` 按开关给出 `(profile, model, reasoning_effort)`：快速 `HARNESS_QUICK_REASONING_EFFORT`（默认 low），深度 `HARNESS_DEEP_REASONING_EFFORT`（默认 high），并可用 `HARNESS_DEEP_MODEL` 换模型。推理强度在运行时 `initialize` 时固化，所以两种档位各持一组运行时；后端日志已按档位区分（`effort=low` / `effort=high`）。

### 5. 会话复用（上下文不再膨胀、重进对话保留过程）

现在会话 id 与后端对话 id 绑定并跨轮复用；上下文、压缩和重试全部由官方 Harness 的 session/compaction/llm-retry 负责，应用层不拼接历史，也没有直连模型回退。过程与思考仍由后端按官方 `session.event` 落库，重进对话可复原小程序的展示。

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
## 积分计费（2026-09-17，Mac，端口 8765）

额度单位从「问答次数」改成「积分」：一轮按真实 token 用量扣分，用户价 = deepseek-flash 真实成本 × 1.5，1 积分 = 0.001 元。

| 检查项 | 结果 |
| --- | --- |
| `py_compile`（main / db / config / credits / harness） | 通过 |
| `tsc --noEmit`（小程序 TS 全量） | 0 错 |
| `scripts/check_mp_helpers.py` | 通过（无数组解构风险写法） |
| `GET /api/me` | `quota={credits_limit:600, credits_used:3, credits_left:597}`，免费试用期内额度 600 |
| `GET /api/pay/plans` | `free.monthly_credits=600`、`free.monthly_credits_after_trial=150`、Plus 3000、Pro 15000，价格未变（Plus 12/30/108，Pro 29/75/258） |
| 真实一轮问答（SSE） | `done` 事件带 `credits=3, credits_used=3, credits_limit=600`；正文与来源正常返回 |
| `usage_logs` 落账 | `deepseek-flash / in 1312 / cache_read 5376 / out 189 / cost 0.002176 元 / credits 3` |
| 单价口径核对 | 1312×1.00 + 5376×0.02 + 189×4.00 = 2175.52（元/百万）→ 0.002176 元，正处空闲时段所以是高峰价的一半；0.002176×1.5÷0.001 = 3.26 → 3 分 |
| 小程序「我的」 | `quotaLabel = 本月积分 3 / 600`；会员方案弹窗显示「每月 15000 积分 · 单文件 300MB」，三档租期（月/季/年）正常 |
| 合规/技巧文案 | `content/legal`（02-data / 05-terms / 07-plan）与 `content/tips/08-plan` 全部改成积分口径，重跑 `import_content.py` 后库内已生效（无「次问答」残留） |

### 顺带修掉的白屏根因：编译产物缺 `@swc/runtime`

开发者工具的 TypeScript 插件对部分文件走 SWC 转换，产物需要 `../../@swc/runtime/_async_to_generator` 等外部 helper，但工具不随包提供，运行时直接报 `module '@swc/runtime/_async_to_generator.js' is not defined`，`Page({...})` 不执行——表现就是「我的页面打不开 / 白屏」、「知识库页面打不开」。

- 补上 `miniprogram/@swc/runtime/`（`_async_to_generator`、`_ts_generator`、`_object_spread`、`_object_spread_props`、`_to_consumable_array` 及常用备用 helper），调用约定 `helper._(...)` 已对齐。
- `project.config.json` 的 `ignoreUploadUnusedFiles` 改为 `false`，避免上传时裁掉只被编译产物引用的 helper。
- 修复后实测：`pages/mine/index` 数据键从 1 个恢复为 29 个、无运行时报错、页面正常渲染；`syncWechatProfile()`（async + try/catch 状态机）调用无异常。

## 组件 wxss 选择器与 HTTP 图片警告清零（2026-09-17）

开发者工具持续报的两类警告已定位到根因并修复。

**1. `Some selectors are not allowed in component wxss`**

真因不只是「警告」：组件 `wxss` 里写 `button.hd-new {}` 属于标签选择器，工具会**整条丢弃**，于是覆盖不了基础库预设的 `wx-button[class]`（特异性 0,1,1），表现就是「新建对话」没边框、历史条目文字居中贴左。

- 修复方式：把按钮类名连写两遍（`.hd-new.hd-new`、`.hd-item.hd-item`），特异性提到 0,2,0，等价于原写法且仍是合法类选择器。
- 涉及 6 个组件 76 处：`app-nav`、`history-drawer`、`pick-bar`、`plan-card`、`share-sheet`、`skill-picker`；`history-drawer` 里 `.hd-panel button::before` 也改成逐个类名罗列。
- 验证：`CSS.getMatchedStylesForNode` 确认计算样式从 `padding:0 / align:center / border:0` 回到 `padding:12rpx 0 / align:left / border:1rpx solid #ececec`；截图确认抽屉外观正常。

**2. `<wx-image>: 图片链接 http://... 不再支持 HTTP 协议`**

`services/media.ts` 的 `localizeImage` 本身存在，但原流程是「先把 http 地址 setData 上屏、再异步换成 `http://usr/...`」，所以每次进「我的」都刷一屏警告。

- 修复方式：先留空（`cover: ''` / `src: ''`），拿到本地路径再回填；原始地址另存到 `coverSource` / `srcSource` 供本地化查询与重试。
- `localizeImage` 对 `http://` 直接走字节下载（`wx.request` + `writeFile`），跳过只支持 https 的 `wx.getImageInfo`。
- 涉及 `pages/mine/index.ts`、`pages/tips/index.ts`、`services/media.ts`。

| 检查项 | 结果 |
| --- | --- |
| `tsc -p tsconfig.json --noEmit` | 0 错 |
| `python3 scripts/check_mp_helpers.py` | 通过 |
| 全仓 grep `button.` 与组件 wxss 标签选择器 | 无残留 |
| `ask、chat、mine、tips、recent、knowledge-detail、document、legal、create、market、skill-edit、search、artifact、share、qa、folder-chat、kb-share` 逐页加载 | 每页 `new warnings: 0` |
| 历史对话抽屉截图 | 「新建对话」有边框胶囊、条目左对齐、无居中错位 |
| 「我的」使用技巧缩略图 | 正常渲染为本地路径 `http://usr/cola-asset-*.png`（75×50） |
| 使用技巧详情页（`?id=upload`） | `loadState=ready`、`cover=http://usr/cola-asset-*.png`、15 个正文块、1 张配图、3 条相关推荐，0 警告 |
| `pages/knowledge-detail/index` 不带 `?id=` 直开 | 回退到默认资料库并正常渲染（修复前停在「资料库加载失败」） |

编码约束已同步到 `README.md`（新增「自定义组件 wxss 的选择器约束」与「图片一律先落到本地再上屏」两节），后续新增组件照此办理。

顺带修掉 `pages/knowledge-detail/index` 的一个空参数缺陷：该页原来直接 `getKnowledgeDetail(query.id)`，缺 `id` 时必然进入 `loadState: 'error'`，显示「资料库加载失败 / 请检查网络后重试」，而网络其实是好的。现在缺 `id` 时先拉资料库列表，优先选中「微信用户的知识库」，拿不到再 `switchTab` 回知识库 tab。

## 积分浮动计价：文案口径对齐（2026-09-17）

背景：积分按请求时刻的官方单价自动取价（高峰/空闲两档），所以**同一段对话在不同时段扣的积分不同**（空闲约 3 分、高峰约 7 分）。这是刻意保留的设计（成本完全转嫁、1.5 倍加价倍率恒定），但旧文案写的是「不随时段或用量变相加价」，与实际行为相反，属于上线风险，已全部改写。

| 文件 | 修改 |
| --- | --- |
| `content/legal/entries/07-plan.md` | 新增「积分随官方计价时段浮动」一条与「变动的只是官方单价本身，1.5 倍加价倍率固定不变」一条；普通问答从「约 3 积分」改为「空闲约 3 分、高峰约 7 分」 |
| `content/legal/entries/02-data.md` | 同上口径 |
| `content/legal/entries/05-terms.md` | 协议条款改为「加价倍率固定，但官方单价按时段浮动，同等用量扣分可能不同，一次普通问答约 3 至 7 积分」 |
| `content/tips/entries/08-plan.md` | 使用技巧改为「一次普通问答约 3～7 积分，差价来自提问时段」，并新增「空闲/高峰具体时段」与「同一份资料反复问会更便宜」两条 |
| `content/legal/README.md` | 写作模板句同步，并明确禁止再写成「不随时段变化」 |
| `README.md` / `server/README.md` | 开发文档区分「加价倍率恒定」与「用户可见积分随时段浮动」 |
| `content/legal/manifest.json` | 版本 `2026.09.17.1`，`updated_at` 2026-09-17；data / terms / plan 三篇的更新/生效日期改为 2026年9月17日 |
| `content/tips/manifest.json` | 版本 `2026.09.17.1`，`updated_at` 2026-09-17 |

验证：

- `GET /api/content/legal/{data,terms,plan}` 返回的正文已含新条款；`legal/plan` 篇头显示「更新日期 2026年9月17日 · 生效日期 2026年9月17日」。
- `GET /api/content/tips/plan` 返回「一次普通问答大约 3～7 积分」与「空闲时段/高峰时段」两条。
- 小程序实际打开 `pages/legal/index?id=plan`、`pages/tips/index?id=plan` 截图校对，`loadState=ready`，0 警告。
- 全仓 grep `不随时段` 只剩「不要写成不随时段变化」的写作说明，无残留歧义表述。
- 前端无硬编码的计价解释文案（只展示服务端下发的 `credits_used / credits_limit`），改文案不需要重新提交小程序。


真实微信登录、支付下单/回调/会员权益、真实模型回答仍需正式参数联调。SSE 测试使用“模型尚未配置”的提示，不代表模型能力已验收。未进行微信真机 UI、负载和公开上线审核验收。本记录只证明后端分离、本地运行和所列接口测试结果。

## 隐私授权：自定义弹窗 + 官方同意按钮（2026-09-17 晚，Mac，端口 8765）——已被下节「官方隐私授权弹窗」取代，保留作过程记录

按微信《小程序隐私协议开发指南》（`framework/user-privacy/PrivacyAuthorize.html`）把隐私授权收敛成一个入口，弹窗内直接展示《小程序用户隐私保护指引》正文。

| 项 | 位置 |
| --- | --- |
| 授权服务（查询 / 弹窗 / 回执 / 被动监听 / 兜底） | `miniprogram/services/privacy.ts` |
| 授权弹窗组件（正文 + 官方同意按钮） | `miniprogram/components/privacy-consent/` |
| 挂载页面 | `pages/chat`、`pages/mine`、`pages/knowledge-detail`（真正会触发隐私接口的三页） |
| 指引正文来源 | `server/content/legal/entries/04-guide.md` → `scripts/import_content.py` → `/api/content/legal/guide` |
| 启动注册 | `miniprogram/app.ts` 内 `registerPrivacyNeedListener()` |

验证：

- `tsc --noEmit` 通过；`scripts/check_mp_helpers.py` 通过；开发者工具「普通编译」无报错。
- 模拟器打开弹窗组件截图：正文 54 行，`metaLine` 为「小程序隐私保护指引 · 更新于 2026年9月17日」，`loadFailed=false`；首屏可见标题、更新日期、导言、注意事项卡片，底部「查看微信官方指引 / 暂不同意 / 同意并继续」三颗按钮完整可点。
- 两个按钮等宽，正文在 `scroll-view` 内滚动，不溢出到页脚（第一版用 `max-height` 时按钮压住正文，已改成固定高度 + `height:0` 的 flex 滚动写法）。
- `pages/chat`、`pages/mine`、`pages/ask` 复跑警告检查：`new warnings: 0`；`pages/knowledge-detail` 挂载组件成功（`privacyComp: true`）、0 警告。
- 账号设置弹层与导入文件弹层截图确认：「查看《小程序用户隐私保护指引》全文」「选择文件前会先展示《小程序用户隐私保护指引》」可见，原生 tabBar 已收起，底部「保存 / 取消」不再被遮住。
- 修掉 `04-guide.md` 里多段引用块中的孤立 `>`：解析器原来把它当成正文段落，会渲染出一行只有「>」的内容；现在 `app/services/content.py` 把只有 `>` 的行当作引用分隔，重新导入后 `GET /api/content/legal/guide` 的 54 个 block 里不再有异常段落。

未验证（需要用户在微信后台配合）：后台「服务内容声明 → 用户隐私保护指引」必须填好指引全文并勾选「收集你的昵称、头像 / 收集你选中的照片或视频信息 / 收集你选中的文件」三项；未勾选时对应接口会直接报 errno 112 或被禁用。开发者工具当前 `wx.getPrivacySetting` 返回 `needAuthorization` 为假，所以模拟器不会自然弹窗，弹窗渲染是用组件方法直接触发后截图的。

## 微信客服（联系客服）（2026-09-17 晚，Mac，端口 8765）

结论先行：**不需要自研聊天窗口**。`<button open-type="contact">` 点击后由微信拉起原生客服会话，这是官方唯一支持的路径；前端不渲染也不存聊天内容，收发全在后端回调里。

接口清单先按官方文档目录核过一遍（`开发 → 服务端 → kf-mgnt`）：该分组下只有 kf-message 与 kf-management 两组，kf-message 共 10 个接口，全部已接；kf-management 的 4 个（registerbusiness / getbusiness / listbusiness / updatebusiness）是服务商给客服子商户用的，普通主体不适用，未接。

| 项 | 位置 |
| --- | --- |
| 客服接口封装（10 个）| `app/services/wechat_kf.py` |
| 消息推送回调 / 账号管理 / 代发消息 | `app/main.py`（`/api/wechat/kf/*`） |
| 消息与会话表 | `app/db.py` 新建 `wx_tokens` / `kf_sessions` / `kf_messages` / `kf_outbox` |
| 前端入口 | `pages/legal/index.wxml`（数据管理、隐私安全等六份说明底部）+ `pages/mine/index.wxml`（一行卡片） |
| 客服图标（自绘耳机） | `assets/icons/cola-set/kefu.svg` + `utils/icons.ts` 的 `kefu` / `support` |
| 回归测试 | `tests/test_wechat_kf.py`（8 项） |

验证：

- `python3 -m unittest discover -s tests -v`：**29 项全部通过**（原 21 项无回归 + 新增 8 项）。
- 真凭证联调（本机 `.env` 已配 `WECHAT_APPID`/`WECHAT_SECRET`）：`GET /cgi-bin/customservice/getkflist` 真实返回 1 个客服账号（昵称 `SPW.`），`getonlinekflist` 返回空——证明 `stable_token` 取凭证 + 缓存 + 账号列表全链路可用。
- 用空 body 探过参数名：`setadmin` / `canceladmin` 确认是 `GET` + 查询参数；官方页面正文里只出现 `kf_openid`、不出现 `kf_account`，实现按 `kf_openid`。`POST /customservice/kfaccount/update` 现返回 48001（未授权），确认该接口已不在现行文档清单内，未接。
- `POST /cgi-bin/message/custom/send` 的文档正文逐字校对：`customservice{ kf_account }`、`miniprogrampage{ title, appid, pagepath, thumb_media_id }`（appid 必填）、`news` 限 1 条、`aimsgcontext{ is_ai_msg }`；实现已按此修正（appid 缺省回退自己的 appid、news 截 1 条、开放 `ai_msg＝true` 标注）。
- 回调链路：临时库 + 临时端口的测试服里，`GET` 验签正确回 `echostr`（签名错返回 403）；明文模式入站消息落库后回一条回执、第二条在冷却窗口内只入库不回（回 `success`）；安全模式入站密文（测试侧用 `cryptography` 独立实现 AES-256-CBC）能解开，回包是密文且 `MsgSignature` 校验通过，签名错时不落库。
- 权限：`/api/wechat/kf/*` 管理接口无口令或口令错一律 403；`/api/wechat/kf/status` 的回包经全串比对不含 Token / AESKey / 管理口令。
- 未配凭证时账号类接口在 502 里直接说明「未配置 WECHAT_APPID」，不发起真实微信请求。
- 小程序：`tsc --noEmit` 通过，`scripts/check_mp_helpers.py` 通过；开发者工具「普通编译」后 `pages/mine/index`、`pages/legal/index?type=data`、`pages/legal/index?type=guide`、`pages/ask/index` 复跑警告检查均为 `new warnings: 0`。
- 截图校对：`/tmp/cola-skill/mine-kf.png`（「我的」里的客服入口，耳机图标与四个快捷入口同行高、同圆角）、`/tmp/cola-skill/legal-data-kf-bottom2.png`（数据管理底部只有**一个**绿色「联系客服」按钮；首次修复前出现过两颗重复按钮，已改为只保留带参数的那颗）。

未验证（需要用户在微信后台配合）：

1. MP 后台「开发管理 → 消息推送」未开启，所以本机 `.env` 的 `WECHAT_KF_TOKEN` / `WECHAT_KF_AES_KEY` 仍为空，`GET /api/wechat/kf/callback` 会返回 503。填入后台同一组值并重启后端即生效（回调行为已在测试服里端到端验证过）。
2. 未做真机点击：模拟器里 `open-type="contact"` 不会真的拉起会话窗口，必须真机（或体验版）验证一次。鸿蒙 OS 不支持该属性。
3. 客服消息额度（用户发消息 5 条 / 48 小时，点菜单、关注公众号、扫码各 3 条 / 1 分钟）未做真实额度压测。

## 隐私授权改用微信「官方隐私授权弹窗」+ 微信头像上传（2026-09-17 深夜，Mac，端口 8765）

### 一、隐私弹窗：不再自绘，交给微信

问题（用户上图对比）：用户看到的一直是自绘弹层，不是微信官方那版「用户隐私保护提示 / 拒绝 / 同意」。

根因：`services/privacy.ts` 注册了 `wx.onNeedPrivacyAuthorization`。官方文档写得很明确——官方弹窗「无需开发者适配开发，自动向 C 端用户展示」；而一旦开发者接管了 `onNeedPrivacyAuthorization`，就必须自己弹窗并回传 `resolve`，官方弹窗就不会出现。我们自绘弹层等于把官方弹窗顶掉了。

改法（把主动权交回平台）：

| 项 | 改前 | 改后 |
| --- | --- | --- |
| `wx.onNeedPrivacyAuthorization` | 注册，拦下并弹自绘组件 | **完全不注册**，让微信自己弹 |
| 自绘授权弹层 | `components/privacy-consent/`（挂 3 个页面） | **删除**（4 个文件 + 3 处 json 注册 + 3 处 wxml 挂载） |
| 显式弹窗 | `openPrivacyConsent()` 先走自绘，没挂组件才退回官方 | 直接 `wx.requirePrivacyAuthorize()`，弹的就是官方弹窗 |
| `app.ts onLaunch` | 调 `registerPrivacyNeedListener()` | 只留注释说明为什么不注册 |

`services/privacy.ts` 现在只剩五个导出（`privacySupported` / `readPrivacySetting` / `requestPrivacyAuthorize` / `ensurePrivacyAuthorized` / `warnPrivacyRequired`），全部走官方链路。

### 二、微信头像：从「不读取」改成「用户点一下选用」

必须要说清一个平台事实（不能含糊）：**微信不允许静默读取头像昵称**。`wx.getUserProfile` 已被官方回收，现行文档里 0 命中；现在唯一受支持的路径是官方「头像昵称填写能力」——

- 头像：`<button open-type="chooseAvatar" bind:chooseavatar="onChooseAvatar">`，回调只给 `e.detail.avatarUrl`，且是本机临时路径；
- 昵称：`<input type="nickname">`，聚焦后键盘上方展示微信昵称，点一下即可选用（官方原话）。

所以「点一次同意就自动填好」在技术上做不到；能做到的最接近体验是「同意后点一下就用微信的」。实现：

| 项 | 位置 |
| --- | --- |
| 头像接口 | `app/main.py`：`POST /api/me/avatar`（≤ 2MB，jpg/png/webp）、`GET /api/avatars/{user_id}` |
| 头像文件目录 | `UPLOAD_DIR/avatars/`（每用户一份，换头像即时删旧文件，不留垃圾） |
| `PATCH /api/me` | `schemas.py` 的 `ProfileUpdate` 两个字段改成可选，未传即保持原值 |
| 前端上传 | `services/api.ts` 新增 `uploadAvatar(filePath)` / `userAvatarUrl(avatar)` |
| 账号设置层 | `pages/mine/index.wxml`：圆形头像按钮（右下相机角标）+ 官方昵称输入框；保存按钮改调 `saveProfile()` |
| 头像渲染 | `pages/mine` 新增 `syncAvatar()`：把后端相对地址补上 base 再本地化（渲染层不接受 `http://` 图片）；未设头像时仍用产品标识 |

隐私申报项不变（三项）：收集你的昵称、头像 / 收集你选中的照片或视频信息 / 收集你选中的文件。

### 验证

- 后端接口实测（真实运行中的 8765）：
  - `POST /api/me/avatar` 上传一张 PNG → `{"avatar":"/api/avatars/<uid>?v=<ts>"}`；
  - `GET /api/avatars/<uid>` → `200 image/png 34406`，字节数与源文件一致；
  - `PATCH /api/me {"nickname":"..."}` → 只改昵称，返回里 `avatar` 仍是刚上传的地址，证明「未传即保持原值」生效；
  - 换头像时旧扩展名文件被删，`uploads/avatars/` 下只留一份；
  - 测试数据已回滚（昵称还原成「微信用户」、删掉测试头像文件），不留脏数据。
- 开发者工具实测头像渲染链路：上传后 `avatarSrc` 从空变成 `http://usr/cola-asset-<hash>.png`（`wx.request` 取字节 → 写本地文件 → 渲染），「我的」资料卡和设置层同时显示该头像；未设头像时回退产品标识。
- 过程中发现并修掉一个时序 bug：`load()` 里 `globalData.user` 比 `setData({user})` 先就绪，首屏 `syncAvatar()` 读到的还是旧值，头像会空一帧；现在 `syncAvatar()` 先读页面数据、再拿 `globalData` 兜底。
- 头像角标初版用绿底 + 黑色相机图标（图标素材是 `#000000` 实心），在深色头像上看不出形状；改成白底 + 细边框 + 淡阴影，深浅头像都能看清。
- `pages/mine`、`pages/ask`、`pages/chat`、`pages/knowledge-detail` 复跑警告检查：`new warnings: 0`。
- `python3 -m unittest discover -s tests -v`：29 项全部通过。
- 小程序：`tsc --noEmit` 通过；`scripts/check_mp_helpers.py` 通过。

### 未验证 / 需要用户配合

1. 开发者工具里 `wx.getPrivacySetting` 的 `needAuthorization` 当前为假，模拟器不会自然弹官方弹窗；要看官方弹窗必须真机，或在「详情 → 本地设置」里清掉授权状态后重试——本次未做真机验收。
2. `open-type="chooseAvatar"` 在模拟器里不会拉起真正的微信头像面板，必须真机验证；本次未做真机点击验收。
3. MP 后台「服务内容声明 → 用户隐私保护指引」仍需勾选上述三项，未勾选时对应接口会报 errno 112。

## 账号设置改两步：官方「同意」按钮 → 官方头像昵称控件（2026-09-17 深夜追加，Mac，端口 8765）

### 用户的意见与这一轮的事实核对

用户又一次对比提出：账号设置还是老样子（一个要打字的昵称输入框），没看到官方那版「用户隐私保护提示 / 拒绝 / 同意」弹窗。
在开发者工具里现场实测了三个接口，结论如下（这几条直接决定了实现方式，不能猜）：

| 接口 / 组件 | 实测结果 | 结论 |
| --- | --- | --- |
| `wx.getPrivacySetting` | `{needAuthorization:false, privacyContractName:'《cola知识库小程序隐私保护指引》'}` | 后台指引已配置；当前微信账号已记录「同意过」 |
| `wx.requirePrivacyAuthorize` | 返回 `success`（不弹窗） | 已同意时才不弹；未同意时弹的就是官方弹窗 |
| `wx.getUserInfo` | `fail {errMsg:'getUserInfo:fail api scope is not declared in the privacy agreement', errno:112}` | **官方耦合按钮 `open-type="getUserInfo|agreePrivacyAuthorization"` 在本小程序必失败**，不能用 |
| `wx.getUserProfile` / `<open-data>` | 前者已回收；后者文档写明「不再返回，展示「微信用户」/ 灰色头像」 | 没有静默拿真实头像昵称的通道 |

所以官方文档 demo 里那颗「同意隐私协议并获取头像昵称信息」按钮在这里拿不到真实数据（`bind:getuserinfo` 回调的 `detail` 与 `wx.getUserInfo` 一致，即匿名数据）。合规且能用的只有官方「头像昵称填写能力」：
`<button open-type="chooseAvatar">` 选头像 + `<input type="nickname">`（键盘上方一键选微信昵称），两者各点一下，不需要打字。

### 改了什么

账号设置弹层拆成两步，第一步就是那颗官方同意按钮：

| 位置 | 改前 | 改后 |
| --- | --- | --- |
| 弹层第一步 | 没有；一打开就是要打字的昵称输入框 | 「用户隐私保护提示」说明块 + `<button open-type="agreePrivacyAuthorization">同意并使用微信头像昵称</button>`，点它弹微信官方弹窗 |
| 弹层第二步 | 昵称 = 输入框（要打字） | 昵称 = 一行设置项「微信昵称 / 一键选用」，整行可点，点一下聚焦官方 `<input type="nickname">`，键盘上方点选微信昵称，全程不用打字 |
| 查看指引 | 跳自建 `pages/legal` | 优先 `wx.openPrivacyContract()` 打开微信官方页（与刚才同意的是同一份），失败才回退自建页 |
| 拒绝后的提示 | `wx.showModal` 自绘弹窗（容易被当成官方弹窗） | 只给一句 toast，不再自绘任何弹窗 |
| 点同意没反应的风险 | — | `bindtap` 兜底：1.2 秒内还没进入第二步就主动 `wx.requirePrivacyAuthorize()` 再弹一次官方弹窗 |

涉及文件：`miniprogram/services/privacy.ts`（新增 `openPrivacyContract()`，`warnPrivacyRequired` 改 toast）、`miniprogram/pages/mine/index.ts`（`openAccountSettings` / `onAgreeTap` / `onPrivacyAgreed` / `enterAccountPick` / `focusNickname`）、`miniprogram/pages/mine/index.wxml`、`miniprogram/pages/mine/index.wxss`。

### 验证（本轮实跑）

- 逻辑层实测：`onAgreeTap()` → 1.2 秒兜底授权 → `privacyNeeded=false` 且 `globalData.privacyGranted=true`；`onPrivacyAgreed()` 同样进入第二步；`focusNickname()` → `nicknameFocus=true`；`closeAccountSettings()` → 弹层关闭并复位。`pages/mine` / `pages/chat` / `pages/knowledge-detail` / `pages/ask` 复跑警告检查：`new warnings: 0`。
- 截图：第二步（选用头像昵称）、第一步（官方同意按钮）各一张，见对话内附图。
- 小程序：`tsc --noEmit` 通过；`scripts/check_mp_helpers.py` 通过。
- 后端本轮未改动；既有用例 `./.venv-x86/bin/python3 -m unittest discover -s tests`：29 项，其中 `test_http.test_stream_and_knowledge_deletion` 1 项失败（无模型 Key 时走到了检索式兜底回答，用例仍期望「尚未完成配置」文案，属既有用例与实现的偏差，与本次前端改动无关，待单独对齐）。

### 仍然只能在真机验证的部分

1. 官方隐私弹窗只在「微信侧未记录同意」时出现。开发者工具的 `needAuthorization` 现在是假，想看到弹窗要先「工具栏 → 清缓存 → 清除授权数据」（或换未同意过的微信号 / 真机体验版）。
2. `open-type="chooseAvatar"` 与 `<input type="nickname">` 的微信侧面板（选头像面板、键盘上方微信昵称）在模拟器里不会真的拉起，需真机各点一次确认。

## 登录门禁与 AI 隐私政策（2026-09-17，当前实现）

本节覆盖前面关于登录入口与账号设置的历史记录，以本节为准：

- `pages/login/index` 是唯一登录入口；没有 token 时 `ensureAuth()` 只回登录页，不再静默 `wx.login`，应用层也会拦截未登录的业务页直达。
- 登录页微信按钮下方勾选《服务协议》《隐私政策》《AI 隐私政策》；已勾选时直接登录并进入问 AI，未勾选时点击按钮先弹底部隐私提示层，点“同意”后继续登录。
- 登录页底部不再展示“cola 统一身份”标识。
- 登录后不强制进入账号设置；用户可在“我的 → 账号设置”用微信官方头像、昵称控件自行选择并保存。
- “我的 → 退出登录”改为底部确认层，确认后清理登录态并回登录页。
- 新增 `server/content/legal/entries/08-ai-privacy.md`，并在清单、法务页入口和文档中同步。

验证：`tsc --noEmit`、`scripts/check_mp_helpers.py`、登录/我的/法务页 WXML 与 WXSS 编译、`tests.test_legal_content`、真实 8 篇 legal 内容导入均通过。
