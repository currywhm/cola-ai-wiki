// Object.assign 语义的 helper：`_extends._({}, a, b)`
function _extends() {
  _extends =
    Object.assign ||
    function (target) {
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
  return _extends.apply(null, arguments)
}

module.exports = _extends
module.exports._ = _extends
module.exports.default = _extends
