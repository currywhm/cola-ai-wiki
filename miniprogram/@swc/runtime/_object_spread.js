// 对象展开 helper：`_object_spread._({}, a, b)`，等价于 babel 的 objectSpread2 简化版
function _object_spread(target) {
  for (var i = 1; i < arguments.length; i++) {
    var source = arguments[i]
    if (source == null) continue
    var keys = Object.keys(Object(source))
    for (var j = 0; j < keys.length; j++) {
      var key = keys[j]
      target[key] = source[key]
    }
  }
  return target
}

module.exports = _object_spread
module.exports._ = _object_spread
module.exports.default = _object_spread
