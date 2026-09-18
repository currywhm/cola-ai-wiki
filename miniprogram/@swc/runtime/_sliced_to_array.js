// 数组解构 helper：`_sliced_to_array._(value, n)`，等价于 tslib __read
function _sliced_to_array(value, limit) {
  if (Array.isArray(value)) return value
  if (value == null) return []
  var out = []
  var iterator = typeof Symbol === 'function' ? value[Symbol.iterator] : undefined
  if (iterator === undefined) return Array.prototype.slice.call(value, 0, limit)
  var step
  var index = 0
  while (!(step = iterator.next()).done) {
    out.push(step.value)
    if (limit && out.length === limit) break
  }
  return out
}

module.exports = _sliced_to_array
module.exports._ = _sliced_to_array
module.exports.default = _sliced_to_array
