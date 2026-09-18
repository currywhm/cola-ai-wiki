// SWC 运行时辅助函数（外部 helper 模式）
//
// 为什么项目里需要这个目录
// ------------------------
// 微信开发者工具的 TypeScript 编译插件在部分版本/部分文件上会走 SWC 转换，
// 生成的代码里是外部 helper 形式：
//
//     var _async_to_generator = require("../../@swc/runtime/_async_to_generator")
//
// 但开发者工具并没有把这个运行时打进小程序包，于是运行时报：
//
//     module '@swc/runtime/_async_to_generator.js' is not defined
//
// 结果是整个页面白屏（历史表现为「我的页面打不开」「知识库页面打不开」）。
// 这里按 tslib / babel 的等价语义补齐这些 helper，require 就能解析到真实模块，
// 页面不会再因为编译产物缺运行时依赖而白屏。
//
// 调用形式都是 `helper._(...)`（SWC 的调用约定），所以每个模块都同时导出
// `module.exports` 与 `module.exports._`。

function _async_to_generator(fn) {
  function asyncGeneratorStep(gen, resolve, reject, next, throwError, key, arg) {
    var info
    try {
      info = gen[key](arg)
    } catch (error) {
      reject(error)
      return
    }
    if (info.done) {
      resolve(info.value)
    } else {
      Promise.resolve(info.value).then(next, throwError)
    }
  }
  return function () {
    var self = this
    var args = arguments
    return new Promise(function (resolve, reject) {
      var gen = fn.apply(self, args)
      function next(value) {
        asyncGeneratorStep(gen, resolve, reject, next, throwError, 'next', value)
      }
      function throwError(error) {
        asyncGeneratorStep(gen, resolve, reject, next, throwError, 'throw', error)
      }
      next(undefined)
    })
  }
}

module.exports = _async_to_generator
module.exports._ = _async_to_generator
module.exports.default = _async_to_generator
