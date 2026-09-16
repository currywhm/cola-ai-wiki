// 已选技能的存储。
//
// 规则（按产品要求）：
// - 用户选了技能就存起来，只要本人没再改，就一直沿用，任何页面都不自动重置；
// - 本地先落地（进入对话立即可用，不依赖网络），再尽量同步到服务端，换设备/重装后也能恢复；
// - 服务端返回 null 表示从未设置过，这时才用本地值去播种；返回空数组是用户的真实选择，必须尊重。
import { getPreferences, savePreferences } from '../services/api'

const LOCAL_KEY = 'llmwiki_skill_prefs'
export const MAX_SKILLS = 8

export function readLocalSkills(): string[] {
  const raw = wx.getStorageSync(LOCAL_KEY)
  if (!Array.isArray(raw)) return []
  return raw.filter((item) => typeof item === 'string' && item).slice(0, MAX_SKILLS)
}

function writeLocalSkills(ids: string[]) {
  try {
    wx.setStorageSync(LOCAL_KEY, ids)
  } catch {
    // 存储写入失败不影响本轮使用
  }
}

/** 用户主动修改：本地立即生效，服务端异步跟进（失败不回滚本地，下次启动会补同步）。 */
export function persistSkills(ids: string[]) {
  const cleaned = Array.from(new Set((ids || []).filter((item) => typeof item === 'string' && item))).slice(0, MAX_SKILLS)
  writeLocalSkills(cleaned)
  savePreferences({ skills: cleaned }).catch(() => undefined)
}

/** 取回上次的选择：服务端优先，其次本地；服务端从未设置过时把本地值播种上去。 */
export async function restoreSkills(): Promise<string[]> {
  const local = readLocalSkills()
  try {
    const remote = await getPreferences()
    if (Array.isArray(remote?.skills)) {
      const ids = remote.skills.filter((item) => typeof item === 'string' && item).slice(0, MAX_SKILLS)
      writeLocalSkills(ids)
      return ids
    }
    if (local.length) savePreferences({ skills: local }).catch(() => undefined)
    return local
  } catch {
    return local
  }
}
