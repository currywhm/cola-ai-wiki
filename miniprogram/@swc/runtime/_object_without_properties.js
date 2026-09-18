// 对象剩余属性 helper：`_object_without_properties._(source, excluded)`
// 等价于 babel 的 objectWithoutProperties：把 source 中除 excluded 以外的键挑出来
function _object_without_properties(source, excluded) {
  if (source == null) return {}
  var target = {}
  var keys = Object.keys(Object(source))
  for (var i = 0; i < keys.length; i++) {
    var key = keys[i]
    if (excluded && excluded.indexOf(key) >= 0) continue
    target[key] = source[key]
  }
  if (typeof Object.getOwnPropertySymbols === 'function') {
    var symbols = Object.getOwnPropertySymbols(source)
    for (var j = 0; j < symbols.length; j++) {
      var symbol = symbols[j]
      if (excluded && excluded.indexOf(symbol) >= 0) continue
      if (!Object.prototype.propertyIsEnumerable.call(source, symbol)) continue
      target[symbol] = source[symbol]
    }
  }
  return target
}

module.exports = _object_without_properties
module.exports._ = _object_without_properties
module.exports.default = _object_without_properties
