from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    code: str = Field(min_length=1)


class ProfileUpdate(BaseModel):
    nickname: str = Field(min_length=1, max_length=80)
    avatar: str = Field(default="", max_length=2048)


class KnowledgeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    icon: str = "library_books"


class ChatRequest(BaseModel):
    knowledge_id: str | None = None
    conversation_id: str | None = None
    # 文件夹隔离问答：传入时检索/整理知识仅限该文件夹内文档
    folder_id: str | None = None
    content: str = Field(min_length=1, max_length=8000)
    model: str = "deepseek-chat"
    mode: str = Field(default="knowledge", pattern="^(knowledge|web)$")
    # quick=直接作答；deep=深度思考（输出思考过程，模型固定为 HARNESS_MODEL=deepseek-flash）
    thinking: str = Field(default="quick", pattern="^(quick|deep)$")
    # 前端技能面板选中的技能（kebab-case slug），后端据此要求 agent 先加载并注入该技能
    skill: str = Field(default="", max_length=40, pattern="^[a-z0-9-]*$")


class PayCreateRequest(BaseModel):
    plan: str = Field(pattern="^(plus_monthly|plus_quarterly|plus_yearly|pro_monthly|pro_quarterly|pro_yearly)$")


class DocumentTagUpdate(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=12)


class ArticleImportRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)
    folder_id: str = Field(default="", max_length=64)


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class DocumentMove(BaseModel):
    folder_id: str = Field(default="", max_length=64)


class ConversationPinUpdate(BaseModel):
    """历史对话的置顶开关：只改当前用户名下的会话。"""

    pinned: bool = True


class ShareSource(BaseModel):
    """分享卡片里的参考出处：只带文件名 / 页码 / 公开网页链接。

    不上传原文片段（quote）与文档 id，避免把私有资料内容写进公开分享记录。
    """

    filename: str = Field(default='', max_length=200)
    page_number: int = Field(default=0, ge=0, le=100000)
    url: str = Field(default='', max_length=2048)


class ShareCreate(BaseModel):
    """用户主动转发一条回答给微信好友时落库的分享卡片。"""

    question: str = Field(default='', max_length=500)
    answer: str = Field(min_length=1, max_length=12000)
    knowledge_name: str = Field(default='', max_length=80)
    sources: list[ShareSource] = Field(default_factory=list, max_length=12)
