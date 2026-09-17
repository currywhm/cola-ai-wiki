from pathlib import Path
import re
from typing import Literal
from urllib.parse import quote

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./data/llmwiki.db"


class Settings(BaseSettings):
    app_env: Literal['development', 'production', 'test'] = 'development'
    app_name: str = "知库资料服务"
    database_url: str = DEFAULT_DATABASE_URL
    # 微信云托管 MySQL 模板直接注入这四项。未显式设置 DATABASE_URL 时，
    # 下面四项目会自动拼成 mysql+aiomysql 连接串。
    mysql_address: str = ""
    mysql_username: str = ""
    mysql_password: str = ""
    mysql_database: str = ""
    upload_dir: str = "./uploads"
    # 运营文案（使用技巧等）存放目录：改文件即可更新，不需要发版。
    content_dir: str = "./content"
    # local 用于本机和普通 Docker 部署；cos 用于微信云托管。
    storage_backend: Literal['local', 'cos'] = 'local'
    cos_bucket: str = ""
    cos_region: str = "ap-shanghai"
    # 微信云托管官方示例使用 Bucket / Region；保留 COS_* 作为兼容配置。
    bucket: str = ""
    region: str = ""
    cos_prefix: str = ""
    # 本地联调 COS 时可直填永久密钥；云托管应留空并走开放接口临时密钥。
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    wechat_openapi_base: str = "http://api.weixin.qq.com"
    jwt_secret: str = "change-me-in-production"
    wechat_appid: str = ""
    wechat_secret: str = ""
    # 微信客服：消息推送的 Token / EncodingAESKey 在 MP 后台「开发管理 → 消息推送」里填，
    # 这里必须与后台保持一致；后台选「安全模式」时 EncodingAESKey 必填。
    # 客服账号管理与会话列表是运营动作，不是普通用户接口，用 KF_ADMIN_TOKEN 单独护住。
    wechat_kf_token: str = ""
    wechat_kf_aes_key: str = ""
    wechat_kf_admin_token: str = ""
    # 客服账号完整格式是「前缀@小程序微信号」，这里只维护小程序微信号那一段。
    wechat_kf_account_suffix: str = ""
    # 收到用户第一条客服消息时回一条回执（不代替人工回复），避免没人值守时用户以为石沉大海。
    wechat_kf_autoreply: bool = True
    # 同一用户在同一小时内只回执一次，之后的消息只入库、不打扰人工客服。
    wechat_kf_autoreply_cooldown_seconds: int = 3600
    wechat_mchid: str = ""
    wechat_mch_serial_no: str = ""
    wechat_pay_private_key_path: str = "./cert/apiclient_key.pem"
    wechat_pay_platform_cert_path: str = "./cert/wechatpay_platform.pem"
    wechat_pay_api_v3_key: str = ""
    wechat_pay_notify_url: str = ""
    wechat_pay_base_url: str = "https://api.mch.weixin.qq.com"
    wechat_payment_mode: Literal['virtual', 'merchant'] = 'virtual'
    wechat_virtual_offer_id: str = ""
    wechat_virtual_app_key: str = ""
    wechat_virtual_env: int = 0
    wechat_virtual_product_id: str = ""
    wechat_virtual_goods_price: int = 0
    wechat_virtual_monthly_product_id: str = ""
    wechat_virtual_monthly_goods_price: int = 0
    wechat_virtual_quarterly_product_id: str = ""
    wechat_virtual_quarterly_goods_price: int = 0
    wechat_virtual_yearly_product_id: str = ""
    wechat_virtual_yearly_goods_price: int = 0
    wechat_virtual_plus_monthly_product_id: str = ""
    wechat_virtual_plus_monthly_goods_price: int = 0
    wechat_virtual_plus_quarterly_product_id: str = ""
    wechat_virtual_plus_quarterly_goods_price: int = 0
    wechat_virtual_plus_yearly_product_id: str = ""
    wechat_virtual_plus_yearly_goods_price: int = 0
    wechat_virtual_notify_url: str = ""
    wechat_virtual_notify_token: str = ""
    wechat_virtual_base_url: str = "https://api.weixin.qq.com"
    # 统一大模型配置（优先于下面的 OpenAI-compatible 供应商配置）。
    # 云端只配置这三项即可切换兼容 OpenAI Chat Completions 的模型服务。
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    chat_models: str = ""
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.chat/v1"
    minimax_model: str = "MiniMax-Text-01"
    harness_enabled: bool = False
    harness_home: str = "./harness-home"
    harness_profile: str = "sdk"
    harness_provider: str = "deepseek-official"
    harness_model: str = "deepseek-v4-flash"
    harness_max_tokens: int = 4096
    # 积分口径：用户侧的额度单位从「问答次数」改成「积分」，积分与 deepseek-flash
    # 的真实 token 成本挂钩，用户价 = 模型成本 × credit_markup（默认 1.5 倍）。
    # 单价默认取 DeepSeek 官方「模型 & 价格」的 deepseek-flash 高峰价（元 / 百万 tokens），
    # 空闲时段官方是半价，代码里按请求时间自动减半（见 services/credits.py）。
    # 调价只改这里或 .env，不用改业务代码。
    credit_markup: float = 1.5
    credit_unit_yuan: float = 0.001
    credit_price_input_hit: float = 0.04
    credit_price_input_miss: float = 2.0
    credit_price_output: float = 8.0
    harness_runtime_mode: Literal['exe', 'node'] = 'exe'
    harness_dsh_bin: str = ""
    # 本地开发：deepseek-harness python SDK / SDK runtime 源码路径。
    # 生产环境可改为 pip install deepseek-harness-sdk（无需配置这两项）。
    harness_sdk_path: str = ""
    harness_runtime_sdk_path: str = ""
    harness_reasoning_effort: str = "low"
    harness_strict: bool = True
    # 多租户隔离：每个用户独立的工作区与 DSH_HOME（sessions/skills/storages/attachments
    # 全部私有），运行时按租户池化复用，空闲回收。`profiles/` 作为部署级只读资产共享。
    harness_workspaces: str = "./harness-workspaces"
    harness_max_runtimes: int = 6
    harness_idle_seconds: int = 1800
    # 深度思考开关真正生效：快速/深度走不同推理强度，必要时深度可换模型
    # （dsh-llm-deepseek 的 reasoningEffort 取值 off | low | high | max）
    harness_quick_reasoning_effort: str = "low"
    harness_deep_reasoning_effort: str = "high"
    harness_deep_model: str = ""
    # 计划模式：执行规划通道启用官方 plan mode，计划以「页面附着卡片」提交评审
    harness_plan_mode: bool = True
    # 对话记忆（参考 deepseek-harness 的 session 持久化 + compaction）：
    # 每轮上下文 = memory_summary（压缩态）+ 最近 memory_recent_messages 条原文；
    # 旧轮次累计超过 memory_compress_chars 字时，用 LLM 滚动压缩进摘要
    memory_recent_messages: int = 8
    memory_compress_chars: int = 3000
    memory_summary_max_chars: int = 600
    web_search_provider: Literal['duckduckgo', 'tavily', 'brave'] = 'duckduckgo'
    web_search_api_key: str = ""
    web_search_base_url: str = "https://api.duckduckgo.com"
    web_search_timeout_seconds: int = 12
    # harness agent 自带 web_search 工具走 Exa（runtime 二进制内置默认 provider），
    # 密钥通过 env EXA_API_KEY 传给 Node 运行时；留空则工具报「认证失败」，
    # 模型会降级为通用回答并声明搜索不可用
    exa_api_key: str = ""
    # 合规文案（隐私保护指引 / 服务协议 / 软件许可 / 数据管理 / 隐私安全 /
    # 会员服务条款 / 关于）里随部署环境变化的字段：只在 .env 维护一处，
    # 导入时替换 content/legal 里的 {{operator}} / {{email_line}} / {{icp_line}}。
    # LEGAL_OPERATOR_NAME 建议填真实运营者名称（个人主体填本人姓名或常用称谓），
    # 留空会退化成通用表述，属于上线前必须补齐的项。
    legal_operator_name: str = ""
    legal_contact_email: str = ""
    legal_icp_number: str = ""
    cors_origins: str = "*"
    model_config = SettingsConfigDict(env_file=BACKEND_ROOT / '.env', extra="ignore")

    @model_validator(mode='after')
    def apply_wechat_cloud_mysql(self):
        """Use the native MYSQL_* variables when DATABASE_URL was not provided."""
        if 'database_url' in self.model_fields_set:
            return self
        values = {
            'MYSQL_ADDRESS': self.mysql_address.strip(),
            'MYSQL_USERNAME': self.mysql_username.strip(),
            'MYSQL_PASSWORD': self.mysql_password,
            'MYSQL_DATABASE': self.mysql_database.strip(),
        }
        if not any(values.values()):
            return self
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError('微信云托管 MySQL 配置不完整，缺少：' + '、'.join(missing))
        address = self.mysql_address.strip()
        if '://' in address:
            address = address.split('://', 1)[1].rstrip('/')
        self.database_url = (
            'mysql+aiomysql://'
            f'{quote(self.mysql_username.strip(), safe="")}:'
            f'{quote(self.mysql_password, safe="")}@{address}/'
            f'{quote(self.mysql_database.strip(), safe="")}?charset=utf8mb4'
        )
        return self

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else BACKEND_ROOT / path

    @property
    def cos_bucket_name(self) -> str:
        return self.cos_bucket.strip() or self.bucket.strip()

    @property
    def cos_region_name(self) -> str:
        if 'cos_region' in self.model_fields_set and self.cos_region.strip():
            return self.cos_region.strip()
        return self.region.strip() or self.cos_region.strip() or 'ap-shanghai'

    @property
    def database_backend(self) -> Literal['sqlite', 'mysql']:
        if self.database_url.startswith('sqlite+aiosqlite:///'):
            return 'sqlite'
        if self.database_url.startswith('mysql+aiomysql://'):
            return 'mysql'
        raise ValueError('DATABASE_URL 仅支持 sqlite+aiosqlite:/// 或 mysql+aiomysql://')

    @property
    def database_path(self) -> Path:
        if self.database_backend != 'sqlite':
            raise ValueError('MySQL 连接没有本地数据库文件路径')
        prefix = 'sqlite+aiosqlite:///'
        return self.resolve_path(self.database_url[len(prefix):])

    def validate_runtime(self) -> None:
        self.wechat_appid = self.wechat_appid.strip()
        self.wechat_secret = self.wechat_secret.strip()
        if len(self.jwt_secret) < 32 or self.jwt_secret in {'change-me-in-production', 'replace-with-a-long-random-secret'}:
            raise ValueError('请运行 python scripts/manage.py setup 生成本机密钥，或设置至少 32 字符的随机 JWT_SECRET')
        if self.app_env == 'production' and (not self.wechat_appid or not self.wechat_secret):
            raise ValueError('生产环境必须配置 WECHAT_APPID 和 WECHAT_SECRET')
        if self.wechat_appid and not re.fullmatch(r'wx[0-9a-fA-F]{16}', self.wechat_appid.strip()):
            raise ValueError('WECHAT_APPID 格式不正确，应为 wx 开头的 18 位小程序 AppID')
        if self.wechat_secret and not re.fullmatch(r'[0-9a-zA-Z]{32}', self.wechat_secret.strip()):
            raise ValueError('WECHAT_SECRET 格式不正确，应为 32 位小程序 AppSecret')
        if self.database_backend == 'mysql' and not self.database_url.startswith('mysql+aiomysql://'):
            raise ValueError('MySQL 部署必须使用 mysql+aiomysql:// 连接串')
        if self.storage_backend == 'cos' and (not self.cos_bucket_name or not self.cos_region_name):
            raise ValueError('COS 存储必须配置 Bucket/Region（或 COS_BUCKET/COS_REGION）')
        if bool(self.cos_secret_id.strip()) != bool(self.cos_secret_key.strip()):
            raise ValueError('COS_SECRET_ID 与 COS_SECRET_KEY 必须同时配置')
        if self.harness_enabled and self.harness_max_tokens < 256:
            raise ValueError('HARNESS_MAX_TOKENS 必须至少为 256')

    @property
    def upload_path(self) -> Path:
        path = self.resolve_path(self.upload_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def content_path(self) -> Path:
        return self.resolve_path(self.content_dir)

    @property
    def cors_list(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def chat_model_list(self) -> list[dict]:
        """前端可选模型清单，来自 CHAT_MODELS（格式：模型值:显示名,模型值:显示名）。"""
        items = []
        for raw in self.chat_models.split(','):
            parts = [p.strip() for p in raw.split(':', 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                items.append({'id': parts[0], 'value': parts[0], 'name': parts[1], 'short': parts[1], 'badge': ''})
        return items


settings = Settings()
