from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_env: Literal['development', 'production', 'test'] = 'development'
    app_name: str = "知库资料服务"
    database_url: str = "sqlite+aiosqlite:///./data/llmwiki.db"
    upload_dir: str = "./uploads"
    jwt_secret: str = "change-me-in-production"
    wechat_appid: str = ""
    wechat_secret: str = ""
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
    harness_runtime_mode: Literal['exe', 'node'] = 'exe'
    harness_dsh_bin: str = ""
    # 本地开发：deepseek-harness python SDK / SDK runtime 源码路径。
    # 生产环境可改为 pip install deepseek-harness-sdk（无需配置这两项）。
    harness_sdk_path: str = ""
    harness_runtime_sdk_path: str = ""
    harness_reasoning_effort: str = "low"
    harness_strict: bool = True
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
    cors_origins: str = "*"
    model_config = SettingsConfigDict(env_file=BACKEND_ROOT / '.env', extra="ignore")

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else BACKEND_ROOT / path

    @property
    def database_path(self) -> Path:
        prefix = 'sqlite+aiosqlite:///'
        if not self.database_url.startswith(prefix):
            raise ValueError('当前版本只支持 sqlite+aiosqlite:/// 文件数据库')
        return self.resolve_path(self.database_url[len(prefix):])

    def validate_runtime(self) -> None:
        if len(self.jwt_secret) < 32 or self.jwt_secret in {'change-me-in-production', 'replace-with-a-long-random-secret'}:
            raise ValueError('请运行 python scripts/manage.py setup 生成本机密钥，或设置至少 32 字符的随机 JWT_SECRET')
        if self.app_env == 'production' and (not self.wechat_appid or not self.wechat_secret):
            raise ValueError('生产环境必须配置 WECHAT_APPID 和 WECHAT_SECRET')
        if self.harness_enabled and self.harness_max_tokens < 256:
            raise ValueError('HARNESS_MAX_TOKENS 必须至少为 256')

    @property
    def upload_path(self) -> Path:
        path = self.resolve_path(self.upload_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

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
