// 输入框技能清单：前端只负责把任务写进输入框，真正的技能加载与注入由后端 harness 完成。
// 问答页与文件夹会话共用这一份清单，保证两个入口的技能名称、说明、模板完全一致。

export interface SkillItem {
  id: string
  name: string
  desc: string
  icon: string
  prompt: string
  /** 后端技能包目录名（app/harness_skills/<harness>/SKILL.md），发送时随请求下发 */
  harness: string
}

export const PLANNER_PLACEHOLDER = '告诉我想做什么，我来规划执行——查询知识、生成PPT、撰写报告、整理知识库……'
export const KNOWLEDGE_PLACEHOLDER = '基于全部知识，或@指定知识进行提问'

// 技能落在真实动作上：选中后把任务写进输入框，仍由用户确认后发送
export const SKILLS: SkillItem[] = [
  { id: 'organize', name: '整理知识库', desc: '把资料整理成结构化知识条目', icon: 'knowledge-pick', harness: 'organize-knowledge', prompt: '把当前知识库里的资料整理成结构化的知识条目，按主题归类并列出要点。' },
  { id: 'report', name: '撰写报告', desc: '基于资料输出一份调研报告', icon: 'book', harness: 'write-report', prompt: '基于当前知识库的资料写一份调研报告，包含背景、结论和关键数据。' },
  { id: 'deck', name: '生成 PPT', desc: '输出分页大纲与每页要点', icon: 'ppt', harness: 'make-deck', prompt: '基于当前知识库的资料输出一份 PPT 大纲，按页给出标题与要点。' },
  { id: 'diagram', name: '知识图解', desc: '把长文梳理成知识结构', icon: 'image', harness: 'knowledge-diagram', prompt: '把当前知识库里的长文整理成一份知识图解，按主题分组并给出层级关系。' },
]
