var SnSourceValidator = Class.create();
SnSourceValidator.prototype = {
    initialize: function() {
        this.constants = new SnSourceConstants();
        this.security = new SnSourceSecurity();
        this.repository = new SnSourceRepository();
    },

    validateBundle: function(body, checkConcurrency) {
        this._assertBodySize(body);
        if (!body || typeof body !== 'object' ||
                !/^[0-9a-f]{32}$/i.test(String(body.bundle_id || '')) ||
                Object.prototype.toString.call(body.changes) !== '[object Array]' ||
                body.changes.length < 1 ||
                body.changes.length > this.constants.MAX_CHANGE_ITEMS) {
            throw this.security.error('invalid_bundle', 'Bundle id and 1-50 changes are required', 400);
        }
        this._assertOnlyKeys(body, ['bundle_id', 'changes'], 'bundle');
        var seen = {};
        var results = [];
        for (var i = 0; i < body.changes.length; i++) {
            var change = body.changes[i];
            this._validateChange(change);
            var target = change.operation === 'create' ?
                'create:' + i :
                change.artifact_type + ':' + change.sys_id;
            if (seen[target]) {
                throw this.security.error('duplicate_change', 'Bundle contains duplicate record targets', 400);
            }
            seen[target] = true;
            var result = {index: i, status: 'valid'};
            if (checkConcurrency && change.operation !== 'create') {
                result.concurrency = this.repository.checkChange(change);
            }
            results.push(result);
        }
        return {
            bundle_id: body.bundle_id,
            valid: true,
            change_count: body.changes.length,
            changes: results
        };
    },

    validateExportRequest: function(body) {
        this._assertBodySize(body);
        if (!body || typeof body !== 'object' ||
                Object.prototype.toString.call(body.artifacts) !== '[object Array]' ||
                body.artifacts.length < 1 ||
                body.artifacts.length > this.constants.MAX_EXPORT_ITEMS) {
            throw this.security.error('invalid_export_request', 'Export requires between 1 and 100 artifacts', 400);
        }
        this._assertOnlyKeys(body, ['artifacts'], 'export request');
        for (var i = 0; i < body.artifacts.length; i++) {
            var item = body.artifacts[i];
            if (!item || typeof item !== 'object') {
                throw this.security.error('invalid_export_item', 'Export artifact is invalid', 400);
            }
            this._assertOnlyKeys(
                item,
                ['artifact_type', 'sys_id', 'domain', 'application_scope'],
                'export artifact'
            );
            this.security.artifactConfig(item.artifact_type);
            this.security.requireContext(item);
            this._assertOnlyKeys(item.domain, ['sys_id'], 'domain');
            this._assertOnlyKeys(item.application_scope, ['sys_id'], 'application scope');
        }
        return body.artifacts;
    },

    _validateChange: function(change) {
        if (!change || ['create', 'update', 'delete'].indexOf(change.operation) < 0) {
            throw this.security.error('invalid_operation', 'Change operation must be create, update, or delete', 400);
        }
        this._assertOnlyKeys(
            change,
            ['operation', 'artifact_type', 'sys_id', 'domain', 'application_scope', 'expected', 'values'],
            'change'
        );
        var config = this.security.artifactConfig(change.artifact_type);
        if (config.capabilityCategory !== 'managed_bidirectional') {
            throw this.security.error(
                'artifact_not_writable',
                'Artifact type does not support managed changes',
                400,
                [String(change.artifact_type)]
            );
        }
        this.security.requireContext(change);
        this._assertOnlyKeys(change.domain, ['sys_id'], 'domain');
        this._assertOnlyKeys(change.application_scope, ['sys_id'], 'application scope');
        if (change.operation === 'create') {
            if (change.sys_id || change.expected) {
                throw this.security.error('invalid_create', 'Create must not include sys_id or expected concurrency state', 400);
            }
        } else {
            if (!/^[0-9a-f]{32}$/i.test(String(change.sys_id || ''))) {
                throw this.security.error('invalid_record_id', 'Update and delete require a valid sys_id', 400);
            }
            this._validateExpected(change.expected);
        }
        if (change.operation === 'delete') {
            if (change.values && this._keyCount(change.values) > 0) {
                throw this.security.error('invalid_delete', 'Delete must not include values', 400);
            }
            return;
        }
        if (!change.values || typeof change.values !== 'object' ||
                Object.prototype.toString.call(change.values) === '[object Array]' ||
                this._keyCount(change.values) < 1) {
            throw this.security.error('invalid_values', 'Create and update require a non-empty values object', 400);
        }
        for (var field in change.values) {
            if (change.values.hasOwnProperty(field)) {
                this.security.assertAllowedField(config, field, true);
                this._validateValue(field, change.values[field]);
            }
        }
        if (change.artifact_type === 'scripted_rest_resource' &&
                ((change.operation === 'create' && !change.values.hasOwnProperty('requires_authentication')) ||
                (change.values.hasOwnProperty('requires_authentication') &&
                String(change.values.requires_authentication) !== 'true' &&
                change.values.requires_authentication !== true))) {
            throw this.security.error('authentication_required', 'Scripted REST resources must require authentication', 400);
        }
        if (change.artifact_type === 'script_include' &&
                change.values.hasOwnProperty('client_callable') &&
                (String(change.values.client_callable) === 'true' ||
                change.values.client_callable === true)) {
            throw this.security.error('server_only_required', 'Managed Script Includes must remain server-only', 400);
        }
    },

    _validateExpected: function(expected) {
        if (!expected ||
                !/^\d+$/.test(String(expected.sys_mod_count)) ||
                !/^[0-9a-f]{64}$/i.test(String(expected.hash || '')) ||
                typeof expected.revision !== 'string' ||
                expected.revision.length < 3 ||
                expected.revision.length > 100) {
            throw this.security.error('invalid_concurrency', 'sys_mod_count, revision, and SHA-256 hash are required', 400);
        }
        this._assertOnlyKeys(expected, ['sys_mod_count', 'revision', 'hash'], 'concurrency state');
    },

    _validateValue: function(field, value) {
        if (value === null || typeof value === 'undefined') {
            throw this.security.error('invalid_field_value', 'Null field values are not accepted', 400, [field]);
        }
        if (typeof value === 'object') {
            throw this.security.error('invalid_field_value', 'Field values must be scalar', 400, [field]);
        }
        if (String(value).length > this.constants.MAX_FIELD_CHARS) {
            throw this.security.error('field_value_too_large', 'A field value exceeds the hard limit', 413, [field]);
        }
        if (this.security.containsSensitiveLiteral(value)) {
            throw this.security.error('sensitive_value_rejected', 'Credential-like literal content is not accepted', 400, [field]);
        }
    },

    _assertBodySize: function(body) {
        var serialized;
        try {
            serialized = JSON.stringify(body);
        } catch (ignore) {
            throw this.security.error('invalid_json', 'Request body is not valid JSON', 400);
        }
        if (!serialized || serialized.length > this.constants.MAX_BODY_CHARS) {
            throw this.security.error('body_too_large', 'Request body exceeds the 512 KiB hard limit', 413);
        }
    },

    _keyCount: function(value) {
        var count = 0;
        for (var key in value) {
            if (value.hasOwnProperty(key)) {
                count++;
            }
        }
        return count;
    },

    _assertOnlyKeys: function(value, allowed, label) {
        for (var key in value) {
            if (value.hasOwnProperty(key) && allowed.indexOf(key) < 0) {
                throw this.security.error(
                    'unknown_property',
                    'An unsupported property was supplied for ' + label,
                    400,
                    [key]
                );
            }
        }
    },

    type: 'SnSourceValidator'
};
