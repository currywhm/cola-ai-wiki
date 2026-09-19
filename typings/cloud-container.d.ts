// 微信云托管私有协议调用（wx.cloud.callContainer）在仓库自带的
// miniprogram-api-typings@2.8 里没有声明，这里按官方参数表补齐，
// 业务代码就不用到处写 any，也不会因为类型缺失被迫绕路。
//
// 官方限制（决定了这里的默认值）：
//   · timeout 最大 15s，超过即无效
//   · 请求体 100KiB，返回包 1000KiB
//   · 调用前必须 wx.cloud.init()，基础库 ≥ 2.23.0
interface ICloudContainerParam {
    config: { env: string }
    path: string
    method?: string
    data?: string | Record<string, any> | ArrayBuffer
    header?: Record<string, string>
    dataType?: string
    responseType?: string
    timeout?: number
    success?: (res: ICloudContainerResult) => void
    fail?: (err: IAPIError) => void
    complete?: (res: ICloudContainerResult | IAPIError) => void
}

interface ICloudContainerResult<T = any> {
    statusCode: number
    data: T
    header: Record<string, string>
    callID?: string
    errMsg: string
}

interface WxCloud {
    callContainer<T = any>(param: ICloudContainerParam): Promise<ICloudContainerResult<T>>
}
