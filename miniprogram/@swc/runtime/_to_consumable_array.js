// 数组展开 helper：`_to_consumable_array._(value)`
// 编译产物只用它把数组（或类数组）转成可 concat 的数组，这里保持 babel 的语义
function _to_consumable_array(value) {
  if (Array.isArray(value)) return value.slice()
  if (value == null) return []
  if (typeof value.length === 'number') return Array.prototype.slice.call(value)
  if (typeof Symbol === 'function' && value[Symbol.iterator]) return Array.from(value)
  return [value]
}

module.exports = _to_consumable_array
module.exports._ = _to_consumable_array
module.exports.default = _to_consumable_array
