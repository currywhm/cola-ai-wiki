// 类属性定义 helper：`_define_property._(obj, key, value)`
function _define_property(obj, key, value) {
  if (key in obj) {
    Object.defineProperty(obj, key, {
      value: value,
      enumerable: true,
      configurable: true,
      writable: true,
    })
  } else {
    obj[key] = value
  }
  return obj
}

module.exports = _define_property
module.exports._ = _define_property
module.exports.default = _define_property
