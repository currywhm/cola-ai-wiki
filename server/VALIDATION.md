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

## 验证边界

真实微信登录、支付下单/回调/会员权益、真实模型回答仍需正式参数联调。SSE 测试使用“模型尚未配置”的提示，不代表模型能力已验收。未进行微信真机 UI、负载和公开上线审核验收。本记录只证明后端分离、本地运行和所列接口测试结果。
