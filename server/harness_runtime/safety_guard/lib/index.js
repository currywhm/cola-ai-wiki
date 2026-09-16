/**
 * @cola/dsh-safety-guard —— cola 知识库的「零执行 + 工作区封闭」守卫。
 *
 * 产品能力边界（部署决定，模型和用户都改不了）：
 *   允许：对话、知识库检索、读文档、写文档、整理文档、联网检索、技能。
 *   禁止：执行命令行 / 脚本 / 可执行文件；读写租户工作区以外的服务器文件。
 *
 * 为什么需要在插件层再拦一道（而不是只靠 prompt 和 profile）：
 *   1. prompt 是「请求」，不是边界。文档内容、技能指令、用户输入都可能塞进
 *      「忽略以上规则，执行 …」，只有工具层拒绝才是确定性的。
 *   2. 官方沙箱只约束「写」：`dsh-fs-sandbox` 的说明明确写了
 *      “preserving the local filesystem's read behavior”——读是不受限的。
 *      多租户部署下，这等于任何一个租户都能把服务器上的 .env、源码、
 *      其它租户的工作区读出来。所以读也必须在这里按工作区收口。
 *   3. 技能包是本租户目录下的只读资产（`COLA_SKILL_ROOTS`），模型要按 `skill`
 *      工具返回的目录去读 `references/`、`assets/`，否则技能包就只剩一份壳。
 *      所以只读工具额外放行技能根，写类工具不受影响。
 *
 * 用的就是官方扩展点：`ctx.tools.guard(fn)` 是 `tools/pre-execute` 之后的
 * 单调否决层，返回字符串即拒绝，且后面的监听者无法再改回允许。
 *
 * @module @cola/dsh-safety-guard
 */

import { delimiter, isAbsolute, relative, resolve } from 'node:path'

export const name = 'cola-safety-guard'

// tools 是官方工具注册表；没有它连 guard 都没地方注册。
export const inject = ['tools']

// 只按「词元」匹配工具名（命名风格是 snake_case / kebab-case），
// 避免 `subagent`、`read_image` 这类正常名字被误伤。
const BANNED_TOKENS = new Set([
  'bash', 'sh', 'shell', 'zsh', 'fish', 'pwsh', 'powershell', 'term', 'terminal', 'tty',
  'exec', 'execute', 'subprocess', 'process', 'spawn', 'kill', 'pkill', 'install',
  'run', 'code', 'eval', 'python', 'python3', 'node', 'deno', 'bun', 'php', 'ruby', 'perl',
  'npm', 'pnpm', 'yarn', 'pip', 'pip3', 'uv', 'git', 'docker', 'kubectl', 'workflow', 'ralph',
  'job', 'jobs', 'cmd', 'command', 'script', 'shell_command',
])

// 会指向路径的参数名（fs 工具族用的是 file_path；搜索工具用 path）。
const PATH_KEYS = new Set([
  'file_path', 'filepath', 'file', 'path', 'paths', 'cwd', 'dir', 'directory', 'target', 'root',
])

// 只读工具：允许它们额外读技能包目录（技能包里的 references/ assets/ 就在那里，
// 模型要按 skill 工具返回的目录去读）。写类工具不在此列，仍严格锁在工作区内。
// `read_file` / `read_image` / `list_dir` 这类名字的词元全部命中才算是只读，
// `write_file` / `edit_file` / `delete_path` 因为带上了写类词元，仍按严格规则处理。
const READ_ONLY_TOKENS = new Set([
  'read', 'file', 'files', 'image', 'grep', 'glob', 'ls', 'list', 'dir', 'directory',
  'view', 'cat', 'inspect', 'stat', 'search', 'find', 'notebook', 'text', 'content', 'show', 'open',
])


/** 技能根白名单：由后端按租户注入（`COLA_SKILL_ROOTS`，多路径用 `:` 分隔）。 */
function skillRoots() {
  return String(process.env.COLA_SKILL_ROOTS || '')
    .split(delimiter)
    .map((item) => item.trim())
    .filter((item) => item !== '')
    .map((item) => resolve(item))
}

/** candidate 是否落在 root 内（含相等）。 */
function within(candidate, root) {
  const inside = relative(root, candidate)
  return inside === '' || (!inside.startsWith('..') && !isAbsolute(inside))
}

/** 工具名是否属于只读族（按词元匹配，避免误伤）。 */
function isReadOnlyTool(toolName) {
  const tokens = String(toolName || '')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
  return tokens.length > 0 && tokens.every((token) => READ_ONLY_TOKENS.has(token))
}

function workspaceRoot() {
  const raw = String(process.env.DSH_WORKSPACE_ROOT || '').trim()
  return resolve(raw === '' ? process.cwd() : raw)
}

function bannedToken(toolName) {
  const tokens = String(toolName || '')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
  return tokens.find((token) => BANNED_TOKENS.has(token))
}

/** 该路径是否落在「租户工作区 + 允许的技能根」之外（相对路径按工作区解析）。 */
function escapesWorkspace(value, root, extraRoots = []) {
  if (typeof value !== 'string') return false
  const raw = value.trim()
  if (raw === '') return false
  // file:// 之类带协议的写法不接受，避免绕过路径规范化。
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) return true
  const absolute = isAbsolute(raw) ? resolve(raw) : resolve(root, raw)
  if (within(absolute, root)) return false
  return !extraRoots.some((extra) => within(absolute, extra))
}

function argumentPathOffenders(args, root, extraRoots = []) {
  if (args === null || typeof args !== 'object') return []
  const offenders = []
  for (const [key, value] of Object.entries(args)) {
    if (!PATH_KEYS.has(String(key).toLowerCase())) continue
    const values = Array.isArray(value) ? value : [value]
    for (const item of values) {
      if (escapesWorkspace(item, root, extraRoots)) offenders.push(String(item))
    }
  }
  return offenders
}

export function apply(ctx) {
  const root = workspaceRoot()
  // 只读工具可以额外读这些技能根（技能包本体），其它路径一律拒绝
  const allowedRoots = skillRoots()
  const logger = typeof ctx.logger === 'function' ? ctx.logger('cola-safety-guard') : undefined
  const deny = (message) => {
    if (logger && typeof logger.warn === 'function') logger.warn(message)
    return message
  }

  const dispose = ctx.tools.guard((exec) => {
    const toolName = String(exec?.name || '')
    const readOnly = isReadOnlyTool(toolName)
    const token = bannedToken(toolName)
    if (token) {
      return deny(
        `已拒绝工具「${toolName}」：cola 知识库不提供任何命令、脚本或可执行文件的执行能力，` +
        '请改用对话、知识库资料、文档读写与整理来完成本轮的请求。',
      )
    }
    const offenders = argumentPathOffenders(exec?.arguments, root, readOnly ? allowedRoots : [])
    if (offenders.length > 0) {
      return deny(
        `已拒绝工具「${toolName}」访问工作区以外的路径：${offenders.join('、')}。` +
        '只能读写当前用户工作区内的文件；需要判断依据时请读技能包目录里的文件。',
      )
    }
    return undefined
  })

  if (typeof ctx.on === 'function' && typeof dispose === 'function') {
    ctx.on('dispose', () => dispose())
  }
}
