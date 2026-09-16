# 知库微信小程序

后端已分离到同级独立目录 `../zhi-reader-api/`。`server` 仅为兼容旧路径的符号链接，后端代码、专用虚拟环境和部署说明以独立目录为准。

知库是面向个人资料的智能阅读小程序：文件上传后解析为文本切片，支持关键词检索、引用来源和基于资料库的流式问答。前端为微信原生 TypeScript/WXML/WXSS，服务端为 FastAPI。

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

## 本地联调

```bash
cd server
python3 scripts/manage.py setup
.venv/bin/python scripts/manage.py start
```

小程序开发配置当前使用 `http://127.0.0.1:8765`，接口文档在 `http://127.0.0.1:8765/docs`。`project.private.config.json` 关闭了开发工具的合法域名校验。真机与正式版必须改成外部服务器的 HTTPS 域名，并在微信公众平台配置 request、uploadFile、downloadFile 服务器域名；上线前将 `urlCheck` 恢复为 `true`。

## 外部服务器部署

1. 准备 Linux 服务器、域名和 HTTPS 证书，创建 `/opt/zhi-reader-api`。
2. 上传独立后端源码包 `../zhi-reader-api/dist/zhi-reader-api.tar.gz` 并解压，复制 `.env.example` 为 `.env`。
3. 设置 `JWT_SECRET`、`WECHAT_APPID`、`WECHAT_SECRET`，以及 `DEEPSEEK_API_KEY`（可选 `MINIMAX_API_KEY`）。
4. 执行 `docker compose up -d --build`，反向代理将 `https://api.example.com` 转到 `127.0.0.1:8765`；详细步骤见独立后端的 README。
5. 用 `GET /health` 检查服务；把小程序 `app.ts` 的 `apiBase` 改为 HTTPS API 地址后，在开发者工具「详情 → 域名信息」刷新配置。

服务默认使用 SQLite 与本地 `uploads/`，适合单机部署；生产扩容时把数据库替换为 PostgreSQL，把上传目录替换为 MinIO/S3，并把文档解析任务移到队列。文件解析已支持 PDF、DOCX、TXT、Markdown、CSV 和图片 OCR。Docker 部署会安装 Tesseract 中文/英文语言包；非 Docker 部署请确保系统已安装 `tesseract-ocr` 与 `chi_sim` 语言数据。

## AI 对话实现

对话采用 SSE 增量事件：先发送 `meta`（conversation_id 与 sources），再发送多个 `delta`，最后发送 `done`。模型适配使用 OpenAI-compatible Chat Completions，DeepSeek 与 MiniMax 均可配置。未配置 API Key 时只返回明确的配置提示，不会伪装成真实模型答案。

会话事件的分层、增量输出、可恢复 conversation_id 参考了 `deepseek-harness-master` 的 session/stream 思路；本项目没有把 Harness 桌面端或凭据系统打包进小程序，模型密钥始终只保留在服务端。

## 上线前清单

法律与合规文案（关于 cola 知识库、数据管理、隐私安全、小程序隐私保护指引、用户服务协议、软件许可及服务协议、会员服务条款）统一放在后端 `server/content/legal/`，由 `scripts/import_content.py` 导入数据库、经 `/api/content/legal` 下发到小程序。修改文案不需要重新提交小程序审核；改完之后重跑一次导入即可。清单与占位符说明见 `server/content/legal/README.md`。

- 补齐运营者信息：在 `.env` 里设置 `LEGAL_OPERATOR_NAME`（真实运营者名称）、`LEGAL_CONTACT_EMAIL`、`LEGAL_ICP_NUMBER`，然后重跑导入；漏填可以用 `python scripts/import_content.py --check` 查出来。
- 完成微信小程序主体认证，并在微信后台把《用户隐私保护指引》按 `server/content/legal/README.md` 的清单逐项勾选；开启客服，保证注销与退款有入口。
- 配置真实 `WECHAT_APPID/SECRET`，不要使用开发回退登录。
- 使用 HTTPS 域名、生产 `JWT_SECRET`，并恢复 `urlCheck: true`。
- 配置 DeepSeek/MiniMax Key，设置反向代理超时和上传大小限制。
- 核对虚拟支付道具价格与 `PLAN_CATALOG`、以及 `content/legal/entries/07-plan.md` 三处一致。
- 将 `data/`、`uploads/` 纳入备份，配置日志与异常告警。
