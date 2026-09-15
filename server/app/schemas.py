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
    # quick=直连模型（真流式、秒级首字）；deep=走 harness agent 链路（慢但有工具/多轮能力）
    thinking: str = Field(default="quick", pattern="^(quick|deep)$")


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
