var SnSourceSecurity = Class.create();
SnSourceSecurity.prototype = {
    initialize: function() {
        this.constants = new SnSourceConstants();
    },

    error: function(code, message, status, details) {
        var error = new Error(message);
        error.safeCode = code;
        error.safeStatus = status || 400;
        error.safeDetails = details || [];
        return error;
    },

    requireRole: function(roleKey) {
        var role = this.constants.roles[roleKey];
        if (!role || !gs.hasRole(role)) {
            throw this.error('forbidden', 'The required application role is missing', 403);
        }
    },

    correlationId: function(request) {
        var supplied = request.getHeader('X-Correlation-ID');
        if (supplied && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(String(supplied))) {
            return String(supplied);
        }
        return String(gs.generateGUID());
    },

    artifactConfig: function(key) {
        if (!key || !this.constants.artifactTypes.hasOwnProperty(String(key))) {
            throw this.error('invalid_artifact_type', 'Artifact type is not allowlisted', 400);
        }
        return this.constants.artifactTypes[String(key)];
    },

    isValidRecordId: function(value) {
        return value === 'global' || /^[0-9a-f]{32}$/i.test(String(value || ''));
    },

    requireContext: function(context) {
        if (!context || !context.domain || !context.application_scope) {
            throw this.error('missing_context', 'Explicit domain and application scope are required', 400);
        }
        if (!this.isValidRecordId(context.domain.sys_id)) {
            throw this.error('invalid_domain', 'Domain sys_id is invalid', 400);
        }
        if (!/^[0-9a-f]{32}$/i.test(String(context.application_scope.sys_id || ''))) {
            throw this.error('invalid_scope', 'Application scope sys_id is invalid', 400);
        }
    },

    pageLimit: function(value) {
        var parsed = parseInt(value, 10);
        if (isNaN(parsed)) {
            return this.constants.DEFAULT_PAGE_SIZE;
        }
        if (parsed < 1 || parsed > this.constants.MAX_PAGE_SIZE) {
            throw this.error('invalid_limit', 'Limit must be between 1 and 200', 400);
        }
        return parsed;
    },

    validateCursor: function(value) {
        if (!value) {
            return '';
        }
        if (!/^[0-9a-f]{32}$/i.test(String(value))) {
            throw this.error('invalid_cursor', 'Cursor is invalid', 400);
        }
        return String(value);
    },

    validateContextCursor: function(value) {
        if (!value) {
            return '';
        }
        if (!/^(global|[0-9a-f]{32}):[0-9a-f]{32}$/i.test(String(value))) {
            throw this.error('invalid_cursor', 'Context cursor is invalid', 400);
        }
        return String(value).toLowerCase();
    },

    assertAllowedField: function(config, field, forWrite) {
        var name = String(field || '');
        if (!/^[a-z][a-z0-9_]*$/.test(name) || this.constants.forbiddenFieldPattern.test(name)) {
            throw this.error('forbidden_field', 'A field is not permitted', 400, [name]);
        }
        var allowed = forWrite ? config.writableFields : config.readableFields;
        if (allowed.indexOf(name) < 0) {
            throw this.error('forbidden_field', 'A field is not allowlisted', 400, [name]);
        }
    },

    assertDevelopmentWrite: function() {
        var configured = String(gs.getProperty('x_snagentic_source.environment', 'production')).toLowerCase();
        var installation = String(gs.getProperty('glide.installation.type', '')).toLowerCase();
        var productionFlag = String(gs.getProperty('glide.installation.production', 'true')).toLowerCase();
        if (configured !== 'development' ||
                installation === 'production' || installation === 'prod' ||
                installation === 'test' || installation === 'testing' ||
                productionFlag === 'true') {
            throw this.error('write_environment_denied', 'Writes are permitted only on an explicitly configured development instance', 403);
        }
    },

    canReadRecord: function(record) {
        if (!record.canRead()) {
            throw this.error('record_access_denied', 'Record is not readable', 403);
        }
    },

    canWriteRecord: function(record, operation) {
        var allowed = operation === 'create' ? record.canCreate() :
            (operation === 'delete' ? record.canDelete() : record.canWrite());
        if (!allowed) {
            throw this.error('record_access_denied', 'Record operation is not permitted', 403);
        }
    },

    sanitize: function(value, depth) {
        var currentDepth = depth || 0;
        if (currentDepth > 6) {
            return '[redacted]';
        }
        if (value === null || typeof value === 'undefined') {
            return value;
        }
        if (Object.prototype.toString.call(value) === '[object Array]') {
            var outputArray = [];
            var length = Math.min(value.length, 200);
            for (var i = 0; i < length; i++) {
                outputArray.push(this.sanitize(value[i], currentDepth + 1));
            }
            return outputArray;
        }
        if (typeof value === 'object') {
            var output = {};
            var count = 0;
            for (var key in value) {
                if (!value.hasOwnProperty(key) || count >= 200) {
                    continue;
                }
                if (this.constants.forbiddenFieldPattern.test(String(key))) {
                    output[key] = '[redacted]';
                } else {
                    output[key] = this.sanitize(value[key], currentDepth + 1);
                }
                count++;
            }
            return output;
        }
        if (typeof value !== 'string') {
            return value;
        }
        var text = value;
        text = this.redactText(text);
        return text.length > this.constants.MAX_FIELD_CHARS ?
            text.substring(0, this.constants.MAX_FIELD_CHARS) + '[truncated]' :
            text;
    },

    redactText: function(value) {
        if (value === null || typeof value === 'undefined') {
            return null;
        }
        var text = String(value);
        text = text.replace(
            /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
            '[redacted private key]'
        );
        text = text.replace(
            /\b(password|passwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)\b(\s*[:=]\s*["'])[^"']*(["'])/gi,
            '$1$2[redacted]$3'
        );
        text = text.replace(/\b(Bearer|Basic)\s+[A-Za-z0-9+\/=_\-.:]+/gi, '$1 [redacted]');
        text = text.replace(/(https?:\/\/)[^\/\s:@]+:[^\/\s@]+@/gi, '$1[redacted]@');
        return text;
    },

    containsSensitiveLiteral: function(value) {
        var text = String(value || '');
        return /-----BEGIN [A-Z ]*PRIVATE KEY-----/i.test(text) ||
            /\b(password|passwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*["'][^"']+["']/i.test(text) ||
            /\b(Bearer|Basic)\s+[A-Za-z0-9+\/=_\-.:]+/i.test(text) ||
            /https?:\/\/[^\/\s:@]+:[^\/\s@]+@/i.test(text);
    },

    type: 'SnSourceSecurity'
};
