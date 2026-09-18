// SWC 运行时 helper（外部 helper 模式）——补齐开发者工具没有随包的运行时
//
// 这些文件不是业务代码，是编译产物的运行时依赖：
// 微信开发者工具的 TypeScript 编译插件在部分版本会对文件走 SWC 转换，产出形如
// `require("../../@swc/runtime/_async_to_generator")` 的外部 helper 引用，但工具
// 不会把这些 helper 打进小程序包，导致运行时抛
// `module '@swc/runtime/_async_to_generator.js' is not defined`，页面直接白屏。
//
// 这里补齐等价实现（语义对齐 tslib / babel），让编译产物能找到依赖。
// 新增页面时如果再次遇到同类报错，按报错里的名字在这里补一个同名文件即可，
// 调用形式统一是 `helper._(...)`，所以每个模块都导出 `module.exports._`。
//
// 已提供：_async_to_generator / _ts_generator / _object_spread /
// _object_spread_props / _to_consumable_array / _extends / _define_property /
// _type_of / _sliced_to_array / _object_without_properties / _to_array
