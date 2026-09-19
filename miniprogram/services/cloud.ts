// 小程序与后端之间只有一条通道：微信云托管私有协议（wx.cloud.callContainer）。
//
// 为什么不用 wx.request：
//   · 云托管的默认公网域名无法配置进「服务器域名」，体验版/正式版必然报 url not in domain list；
//   · 自定义域名要买域名 + ICP 备案，个人主体成本高。
// callContainer 走微信内网专线，不需要配置服务器域名，也没有备案问题。
//
// 官方硬限制（所有封装都按它设计，别在这里放宽）：
//   · 单次调用超时上限 15s        → 长耗时动作必须改成后台任务 + 轮询
//   · 请求体上限 100KiB           → 文件上传走对象存储直传
//   · 返回包上限 1000KiB          → 大 JSON 要瘦身、二进制走签名地址
//   · 调用前必须 wx.cloud.init()  → 见 initCloud()，基础库需 ≥ 2.23.0
export const CLOUD_ENV = 'prod-d4g10pz8j04137ed3'
// 服务名必须与「云托管控制台 → 服务管理 → 服务列表」里的名称逐字一致，否则网关路由不到
export const CLOUD_SERVICE = 'cola-ai-wiki-dev3'
export const CONTAINER_TIMEOUT = 15000

export type ContainerResponse<T> = {
  statusCode: number
  data: T
  header: Record<string, string>
}

export type ContainerMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

type ContainerParam = {
  path: string
  method?: ContainerMethod
  data?: any
  header?: Record<string, string>
  timeout?: number
}

function cloud(): any {
  return (wx as any).cloud
}

/** 全局初始化一次即可；基础库过低时吞掉异常，调用阶段再给出可读提示。 */
export function initCloud(): void {
  try {
    const api = cloud()
    if (api && typeof api.init === 'function') api.init({ env: CLOUD_ENV, traceUser: true })
  } catch (error) {
    // 忽略：调用 callContainer 时若确实不可用，会给出「请升级微信」的提示
  }
}

function notEnabled(error: any): boolean {
  const message = String((error && (error.errMsg || error.message)) || '')
  return message.indexOf("Cloud API isn't enabled") >= 0
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 云托管调用。返回体与 wx.request 一致（statusCode + data），
 * 所以上层的错误映射、401 重试逻辑都不用改。
 */
export function callContainer<T>(param: ContainerParam, retry = 1): Promise<ContainerResponse<T>> {
  const api = cloud()
  if (!api || typeof api.callContainer !== 'function') {
    return Promise.reject(Object.assign(new Error('当前微信版本不支持云托管调用，请升级微信后重试'), { unsupported: true }))
  }
  const timeout = Math.min(param.timeout || CONTAINER_TIMEOUT, CONTAINER_TIMEOUT)
  return api.callContainer({
    config: { env: CLOUD_ENV },
    path: param.path,
    method: param.method || 'GET',
    data: param.data,
    header: Object.assign({ 'X-WX-SERVICE': CLOUD_SERVICE }, param.header || {}),
    timeout,
  }).catch((error: any) => {
    // init 异步生效：极少数情况下首次调用会早于 init 完成，等 300ms 重试一次即可
    if (retry > 0 && notEnabled(error)) return wait(300).then(() => callContainer<T>(param, retry - 1))
    throw error
  })
}
