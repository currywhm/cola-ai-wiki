// typeof helper：`_type_of._(value)`
function _type_of(value) {
  if (typeof Symbol === 'function' && typeof Symbol.iterator === 'symbol') {
    _type_of = function (inner) {
      return inner === null ? 'null' : typeof inner === 'object' && typeof inner[Symbol.iterator] === 'function' ? 'symbol' : typeof inner
    }
  } else {
    _type_of = function (inner) {
      return inner === null ? 'null' : typeof inner
    }
  }
  return _type_of(value)
}

module.exports = _type_of
module.exports._ = _type_of
module.exports.default = _type_of
