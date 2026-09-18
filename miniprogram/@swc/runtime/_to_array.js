// 数组转换 helper：`_to_array._(value)`，等价于 tslib __spreadArray 的输入转换
function _to_array(value) {
  if (Array.isArray(value)) return value.slice()
  if (value == null) return []
  if (typeof Symbol === 'function' && value[Symbol.iterator]) return Array.from(value)
  return Array.prototype.slice.call(value)
}

module.exports = _to_array
module.exports._ = _to_array
module.exports.default = _to_array
