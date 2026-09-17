from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    code: str = Field(min_length=1)


class ProfileUpdate(BaseModel):
    """两个字段都可选：只改昵称时不会顺手把头像清掉，反之亦然。"""
    nickname: str | None = Field(default=None, min_length=1, max_length=80)
    avatar: str | None = Field(default=None, max_length=2048)


class PreferenceUpdate(BaseModel):
    """用户偏好：只允许白名单字段，未传的字段保持不变。"""

    # 用户当前启用的技能 id：只有用户主动修改才写入，后端不会自动重置
    skills: list[str] | None = Field(default=None, max_length=8)


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
    # 留空时由后端从 LLM_MODEL / DEEPSEEK_MODEL 等环境配置中取值。
    model: str = ""
    mode: str = Field(default="knowledge", pattern="^(knowledge|web)$")
    # quick=直接作答；deep=深度思考（输出思考过程，模型固定为 HARNESS_MODEL=deepseek-flash）
    thinking: str = Field(default="quick", pattern="^(quick|deep)$")
    # 前端技能面板选中的技能（kebab-case slug），后端据此要求 agent 先加载并注入该技能
    skill: str = Field(default="", max_length=40, pattern="^[a-z0-9-]*$")
    # 多选技能：优先于 skill；为空时回落到 skill（兼容旧客户端）
    skills: list[str] = Field(default_factory=list, max_length=8)
    # 计划模式：显式打开官方 plan mode（计划先评审、批准后再执行）。
    # 不传时由后端按 HARNESS_PLAN_MODE 与问答通道决定。
    plan: bool | None = None


class PlanReviewRequest(BaseModel):
    """计划评审结论：批准 / 继续规划（可带反馈）。"""

    review_id: str = Field(min_length=1, max_length=120)
    approved: bool = False
    feedback: str = Field(default="", max_length=2000)


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


class ShareFile(BaseModel):
    """分享卡片里带出去的文件：只存产物 id 与展示名。

    公开页凭分享 id 取文件，产物 id 与用户身份不出现在分享内容里。
    """

    artifact_id: str = Field(min_length=4, max_length=64)
    name: str = Field(default='', max_length=200)


class ShareCreate(BaseModel):
    """用户把选中的回答与文件转发给微信好友时落库的分享卡片。

    只保存正文与出处文件名，不保存分享者的昵称 / 头像 / openid。
    """

    title: str = Field(default='', max_length=120)
    question: str = Field(default='', max_length=500)
    answer: str = Field(min_length=1, max_length=12000)
    knowledge_name: str = Field(default='', max_length=80)
    sources: list[ShareSource] = Field(default_factory=list, max_length=12)
    files: list[ShareFile] = Field(default_factory=list, max_length=8)


class ShareToKnowledge(BaseModel):
    """把对话里选中的内容与文件存进指定知识库。"""

    knowledge_id: str = Field(min_length=4, max_length=64)
    folder_id: str = Field(default='', max_length=64)
    title: str = Field(default='', max_length=120)
    content: str = Field(default='', max_length=60000)
    artifact_ids: list[str] = Field(default_factory=list, max_length=12)


class SkillForm(BaseModel):
    """新建 / 编辑「我的技能」。"""

    name: str = Field(min_length=1, max_length=30)
    summary: str = Field(default='', max_length=60)
    prompt: str = Field(min_length=1, max_length=4000)
    developer_wechat: str = Field(default='', max_length=40)
    icon: str = Field(default='skill-node', max_length=40)


class SkillBuildRequest(BaseModel):
    """新建技能：把用户口述的要求交给 harness 的 skill-creator 做成技能包。"""

    instruction: str = Field(min_length=4, max_length=4000)
    name: str = Field(default='', max_length=30)
    summary: str = Field(default='', max_length=60)
    icon: str = Field(default='skill-node', max_length=40)
    developer_wechat: str = Field(default='', max_length=40)


class SkillEnhanceRequest(BaseModel):
    """增强提示词：按技能模板把用户随手写的一句话改写成型。"""

    instruction: str = Field(min_length=2, max_length=4000)
    name: str = Field(default='', max_length=30)


class SkillPublishUpdate(BaseModel):
    """发布到技能广场 / 从广场下架。"""

    published: bool = True


class SkillFlagUpdate(BaseModel):
    """点赞 / 收藏的开与关。"""

    active: bool = True
