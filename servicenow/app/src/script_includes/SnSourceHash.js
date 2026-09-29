var SnSourceHash = Class.create();
SnSourceHash.prototype = {
    initialize: function() {},

    canonicalStringify: function(value) {
        if (value === null || typeof value === 'undefined') {
            return 'null';
        }
        if (Object.prototype.toString.call(value) === '[object Array]') {
            var arrayParts = [];
            for (var i = 0; i < value.length; i++) {
                arrayParts.push(this.canonicalStringify(value[i]));
            }
            return '[' + arrayParts.join(',') + ']';
        }
        if (typeof value === 'object') {
            var keys = [];
            var key;
            for (key in value) {
                if (value.hasOwnProperty(key)) {
                    keys.push(key);
                }
            }
            keys.sort();
            var objectParts = [];
            for (var j = 0; j < keys.length; j++) {
                key = keys[j];
                objectParts.push(JSON.stringify(key) + ':' + this.canonicalStringify(value[key]));
            }
            return '{' + objectParts.join(',') + '}';
        }
        return JSON.stringify(value);
    },

    sha256: function(value) {
        var digest = new GlideDigest();
        return String(digest.getSHA256Hex(this.canonicalStringify(value))).toLowerCase();
    },

    type: 'SnSourceHash'
};
