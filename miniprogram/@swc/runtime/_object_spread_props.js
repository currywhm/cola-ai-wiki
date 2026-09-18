// 具名属性合并 helper：`_object_spread_props._(target, props)`
// 语义同 babel 的 objectSpread2 第二参数：把 props 的可枚举属性覆盖到 target 上
function _object_spread_props(target, props) {
  if (props == null) return target
  var keys = Object.keys(Object(props))
  for (var i = 0; i < keys.length; i++) {
    var key = keys[i]
    target[key] = props[key]
  }
  return target
}

module.exports = _object_spread_props
module.exports._ = _object_spread_props
module.exports.default = _object_spread_props
