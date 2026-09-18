"""Knowledge-market publishing policy and automatic category selection.

The public market is intentionally conservative: authors must acknowledge the
policy and content is checked before publication.  The same check is reused
when a published knowledge base receives more material, so a previously clean
knowledge base cannot silently become public with newly added content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


CATEGORY_LABELS = (
    "科技",
    "教育",
    "职场",
    "财经",
    "产业",
    "健康",
    "法律",
    "生活",
    "其他",
)

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "科技": (
        "人工智能", "生成式ai", "大模型", "机器学习", "深度学习", "算法", "编程",
        "软件开发", "互联网", "云计算", "大数据", "数据库", "芯片", "半导体",
        "机器人", "自动化", "网络安全", "信息技术", "科技", "数字化", "计算机",
    ),
    "教育": (
        "教育", "教学", "课程", "课堂", "学校", "大学", "考试", "学习", "培训",
        "教师", "学生", "教材", "升学", "知识", "学术", "论文",
    ),
    "职场": (
        "职场", "招聘", "面试", "简历", "绩效", "薪酬", "人力资源", "员工",
        "团队管理", "领导力", "职业发展", "岗位", "办公", "项目管理", "协作",
    ),
    "财经": (
        "金融", "投资", "股票", "证券", "基金", "银行", "保险", "财务", "会计",
        "经济", "财税", "审计", "融资", "并购", "财报", "资产管理", "风险控制",
    ),
    "产业": (
        "产业", "制造业", "工业", "供应链", "物流", "工厂", "能源", "新能源",
        "汽车", "医药", "化工", "材料", "房地产", "零售", "消费", "农业",
    ),
    "健康": (
        "健康", "医疗", "疾病", "诊断", "治疗", "药品", "医学", "医生", "护理",
        "营养", "心理", "睡眠", "康复", "运动医学", "公共卫生",
    ),
    "法律": (
        "法律", "法规", "法条", "合同", "协议", "诉讼", "仲裁", "律师", "法院",
        "劳动法", "民法典", "知识产权", "合规", "监管", "案例", "司法解释",
    ),
    "生活": (
        "生活", "旅行", "旅游", "美食", "烹饪", "家庭", "育儿", "亲子", "宠物",
        "运动", "健身", "摄影", "整理", "收纳", "穿搭", "兴趣爱好",
    ),
}


@dataclass(frozen=True)
class PolicyFinding:
    code: str
    message: str


# These are high-signal launch checks, not a claim that a finite word list can
# replace human review.  Authors still acknowledge the policy and operators can
# unpublish content at any time.
_POLICY_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "state_secret",
        "内容疑似涉及国家秘密、涉密材料或未公开敏感文件，不能发布到知识库广场。",
        (
            "国家秘密", "绝密文件", "绝密资料", "机密文件", "机密资料", "秘密级文件",
            "涉密文件", "涉密资料", "内部机密", "国家机密", "泄露国家秘密", "窃取国家秘密",
        ),
    ),
    (
        "illegal_violence",
        "内容疑似涉及暴力、恐怖活动或违法犯罪方法，不能发布到知识库广场。",
        (
            "制作炸药", "制造炸弹", "爆炸物配方", "枪支买卖", "毒品交易", "制毒方法",
            "恐怖袭击", "发动暴恐", "杀人方法", "雇凶杀人", "绑架教程", "投毒教程",
        ),
    ),
    (
        "national_security",
        "内容疑似涉及分裂国家、颠覆国家政权或危害国家安全，不能发布到知识库广场。",
        (
            "分裂国家", "颠覆国家政权", "推翻政府", "煽动颠覆", "台独", "港独", "疆独", "藏独",
            "反党反华", "反华组织", "危害国家安全", "叛国组织",
        ),
    ),
    (
        "defamation",
        "内容疑似攻击、侮辱、抹黑国家领导人或英雄烈士，不能发布到知识库广场。",
        (
            "攻击国家领导人", "抹黑国家领导人", "侮辱国家领导人", "诋毁国家领导人",
            "攻击领导人", "抹黑领导人", "侮辱英烈", "诋毁英烈", "抹黑英雄",
            "诋毁英雄", "污蔑英雄",
        ),
    ),
    (
        "malicious_rumor",
        "内容疑似捏造事实、制造谣言或煽动社会对立，不能发布到知识库广场。",
        (
            "捏造事实", "编造谣言", "传播谣言", "造谣国家", "煽动仇恨", "煽动民族仇恨",
            "煽动地域仇恨", "制造社会对立",
        ),
    ),
)


def _normalized(value: str) -> str:
    return re.sub(r"[\s,，。；;、:：!！?？'\"“”‘’（）()\[\]【】<>《》/\\|_-]+", "", (value or "").lower())


def review_content(*parts: str, limit: int = 1) -> list[PolicyFinding]:
    """Return policy findings found in the supplied content."""

    text = _normalized("\n".join(str(part or "") for part in parts))
    findings: list[PolicyFinding] = []
    for code, message, terms in _POLICY_PATTERNS:
        if any(_normalized(term) in text for term in terms):
            findings.append(PolicyFinding(code=code, message=message))
            if len(findings) >= max(1, limit):
                break
    return findings


def classify_content(*parts: str) -> str:
    """Choose a market category from weighted keyword signals.

    Title and description are supplied before document metadata by callers, so
    the same keyword receives a natural early-position boost.
    """

    scores = {label: 0 for label in CATEGORY_LABELS if label != "其他"}
    for part_index, part in enumerate(parts):
        text = _normalized(part)
        if not text:
            continue
        weight = 5 if part_index == 0 else 3 if part_index == 1 else 1
        for label, keywords in CATEGORY_KEYWORDS.items():
            for keyword in keywords:
                term = _normalized(keyword)
                if term and term in text:
                    scores[label] += weight
    winner = max(scores, key=lambda label: scores[label])
    return winner if scores[winner] > 0 else "其他"
