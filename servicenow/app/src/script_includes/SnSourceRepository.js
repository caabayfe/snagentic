var SnSourceRepository = Class.create();
SnSourceRepository.prototype = {
    initialize: function() {
        this.constants = new SnSourceConstants();
        this.security = new SnSourceSecurity();
        this.hash = new SnSourceHash();
    },

    listDomains: function(limit, cursor) {
        var records = [];
        var gr = new GlideRecord('sys_domain');
        if (!gr.isValid()) {
            throw this.security.error('domain_table_unavailable', 'Domain hierarchy is unavailable', 501);
        }
        if (cursor) {
            gr.addQuery('sys_id', '>', cursor);
        }
        gr.orderBy('sys_id');
        gr.setLimit(limit + 1);
        gr.query();
        var hasMore = false;
        while (gr.next()) {
            if (records.length === limit) {
                hasMore = true;
                break;
            }
            this.security.canReadRecord(gr);
            var parentId = gr.getValue('parent') || '';
            records.push({
                sys_id: gr.getUniqueValue(),
                name: gr.getValue('name') || '',
                path: gr.isValidField('sys_domain_path') ? (gr.getValue('sys_domain_path') || '') : '',
                parent: parentId ? {
                    sys_id: parentId,
                    name: gr.getDisplayValue('parent') || ''
                } : null
            });
        }
        return {
            items: records,
            next_cursor: hasMore && records.length ? records[records.length - 1].sys_id : null
        };
    },

    listContexts: function(limit, cursor) {
        var contexts = {};
        for (var type in this.constants.artifactTypes) {
            if (!this.constants.artifactTypes.hasOwnProperty(type)) {
                continue;
            }
            this._collectContexts(
                this.constants.artifactTypes[type],
                contexts
            );
        }

        var keys = [];
        for (var key in contexts) {
            if (contexts.hasOwnProperty(key) && (!cursor || key > cursor)) {
                keys.push(key);
            }
        }
        keys.sort();

        var items = [];
        var pageLength = Math.min(keys.length, limit);
        for (var i = 0; i < pageLength; i++) {
            items.push(contexts[keys[i]]);
        }
        return {
            items: items,
            next_cursor: keys.length > limit && items.length ?
                keys[items.length - 1] :
                null
        };
    },

    listArtifacts: function(type, domainId, scopeId, limit, cursor) {
        var config = this.security.artifactConfig(type);
        var gr = new GlideRecord(config.table);
        this._assertTable(gr);
        this._addContextQuery(gr, domainId, scopeId);
        if (cursor) {
            gr.addQuery('sys_id', '>', cursor);
        }
        gr.orderBy('sys_id');
        gr.setLimit(limit + 1);
        gr.query();
        var records = [];
        var hasMore = false;
        while (gr.next()) {
            if (records.length === limit) {
                hasMore = true;
                break;
            }
            records.push(this._serialize(gr, type, config, false));
        }
        return {
            items: records,
            next_cursor: hasMore && records.length ? records[records.length - 1].sys_id : null
        };
    },

    exportArtifacts: function(items) {
        if (!items || Object.prototype.toString.call(items) !== '[object Array]' ||
                items.length < 1 || items.length > this.constants.MAX_EXPORT_ITEMS) {
            throw this.security.error('invalid_export_items', 'Export must contain between 1 and 100 artifacts', 400);
        }
        var output = [];
        var seen = {};
        for (var i = 0; i < items.length; i++) {
            var item = items[i] || {};
            this.security.requireContext(item);
            if (!/^[0-9a-f]{32}$/i.test(String(item.sys_id || ''))) {
                throw this.security.error('invalid_record_id', 'Artifact sys_id is invalid', 400);
            }
            var key = String(item.artifact_type) + ':' + String(item.sys_id);
            if (seen[key]) {
                throw this.security.error('duplicate_artifact', 'Export contains a duplicate artifact', 400);
            }
            seen[key] = true;
            output.push(this.getArtifact(
                item.artifact_type,
                item.sys_id,
                item.domain.sys_id,
                item.application_scope.sys_id
            ));
        }
        return output;
    },

    getArtifact: function(type, sysId, domainId, scopeId) {
        var config = this.security.artifactConfig(type);
        var gr = new GlideRecord(config.table);
        this._assertTable(gr);
        this._addContextQuery(gr, domainId, scopeId);
        gr.addQuery('sys_id', sysId);
        gr.setLimit(1);
        gr.query();
        if (!gr.next()) {
            throw this.security.error('artifact_not_found', 'Artifact was not found in the requested context', 404);
        }
        return this._serialize(gr, type, config, true);
    },

    checkChange: function(change) {
        if (change.operation === 'create') {
            return {status: 'ready', current: null};
        }
        var current = this.getArtifact(
            change.artifact_type,
            change.sys_id,
            change.domain.sys_id,
            change.application_scope.sys_id
        );
        var expected = change.expected;
        if (String(expected.sys_mod_count) !== String(current.sys_mod_count) ||
                String(expected.revision) !== current.revision ||
                String(expected.hash).toLowerCase() !== current.hash) {
            throw this.security.error(
                'concurrency_conflict',
                'Artifact changed after the bundle was prepared',
                409,
                [{artifact_type: change.artifact_type, sys_id: change.sys_id}]
            );
        }
        return {
            status: 'ready',
            current: {
                sys_mod_count: current.sys_mod_count,
                revision: current.revision,
                hash: current.hash
            }
        };
    },

    applyChange: function(change, correlationId) {
        if (change.operation === 'create') {
            return this._create(change);
        }
        var current = this.getArtifact(
            change.artifact_type,
            change.sys_id,
            change.domain.sys_id,
            change.application_scope.sys_id
        );
        this.checkChange(change);
        if (change.operation === 'delete') {
            return this._delete(change, current, correlationId);
        }
        return this._update(change);
    },

    listTombstones: function(domainId, scopeId, limit, cursor) {
        var gr = new GlideRecord('x_snagentic_source_tombstone');
        this._assertTable(gr);
        gr.addQuery('domain_id', domainId);
        gr.addQuery('scope_id', scopeId);
        gr.addQuery('state', 'complete');
        if (cursor) {
            gr.addQuery('sys_id', '>', cursor);
        }
        gr.orderBy('sys_id');
        gr.setLimit(limit + 1);
        gr.query();
        var records = [];
        var hasMore = false;
        while (gr.next()) {
            if (records.length === limit) {
                hasMore = true;
                break;
            }
            this.security.canReadRecord(gr);
            records.push({
                sys_id: gr.getUniqueValue(),
                artifact_type: gr.getValue('artifact_type'),
                record_sys_id: gr.getValue('record_sys_id'),
                record_name: gr.getValue('record_name'),
                domain: {sys_id: gr.getValue('domain_id')},
                application_scope: {sys_id: gr.getValue('scope_id')},
                prior_sys_mod_count: parseInt(gr.getValue('prior_sys_mod_count'), 10),
                prior_revision: gr.getValue('prior_revision'),
                prior_hash: gr.getValue('prior_hash'),
                deleted_at: gr.getValue('deleted_at'),
                deleted_by: gr.getValue('deleted_by'),
                correlation_id: gr.getValue('correlation_id')
            });
        }
        return {
            items: records,
            next_cursor: hasMore && records.length ? records[records.length - 1].sys_id : null
        };
    },

    _create: function(change) {
        var config = this.security.artifactConfig(change.artifact_type);
        var gr = new GlideRecord(config.table);
        this._assertTable(gr);
        gr.initialize();
        this._setContext(gr, change.domain.sys_id, change.application_scope.sys_id);
        this._setValues(gr, config, change.values);
        this.security.canWriteRecord(gr, 'create');
        var insertedId = gr.insert();
        if (!insertedId) {
            throw this.security.error('create_failed', 'Artifact could not be created', 500);
        }
        return this.getArtifact(
            change.artifact_type,
            String(insertedId),
            change.domain.sys_id,
            change.application_scope.sys_id
        );
    },

    _update: function(change) {
        var config = this.security.artifactConfig(change.artifact_type);
        var gr = new GlideRecord(config.table);
        this._assertTable(gr);
        this._addContextQuery(gr, change.domain.sys_id, change.application_scope.sys_id);
        gr.addQuery('sys_id', change.sys_id);
        gr.addQuery('sys_mod_count', String(change.expected.sys_mod_count));
        gr.addQuery('sys_updated_on', this._revisionTimestamp(change.expected.revision));
        gr.setLimit(1);
        gr.query();
        if (!gr.next()) {
            throw this.security.error('concurrency_conflict', 'Artifact changed after the bundle was prepared', 409);
        }
        this.security.canWriteRecord(gr, 'update');
        this._setValues(gr, config, change.values);
        if (!gr.update()) {
            throw this.security.error('update_failed', 'Artifact could not be updated', 500);
        }
        return this.getArtifact(
            change.artifact_type,
            change.sys_id,
            change.domain.sys_id,
            change.application_scope.sys_id
        );
    },

    _delete: function(change, current, correlationId) {
        var config = this.security.artifactConfig(change.artifact_type);
        var gr = new GlideRecord(config.table);
        this._assertTable(gr);
        this._addContextQuery(gr, change.domain.sys_id, change.application_scope.sys_id);
        gr.addQuery('sys_id', change.sys_id);
        gr.addQuery('sys_mod_count', String(change.expected.sys_mod_count));
        gr.addQuery('sys_updated_on', this._revisionTimestamp(change.expected.revision));
        gr.setLimit(1);
        gr.query();
        if (!gr.next()) {
            throw this.security.error('concurrency_conflict', 'Artifact changed after the bundle was prepared', 409);
        }
        this.security.canWriteRecord(gr, 'delete');
        var tombstoneId = this._writeTombstone(change, current, correlationId);
        if (!gr.deleteRecord()) {
            throw this.security.error('delete_failed', 'Artifact could not be deleted', 500);
        }
        this._completeTombstone(tombstoneId);
        return {
            artifact_type: change.artifact_type,
            sys_id: change.sys_id,
            operation: 'delete',
            tombstoned: true,
            domain: current.domain,
            application_scope: current.application_scope
        };
    },

    _writeTombstone: function(change, current, correlationId) {
        var tombstone = new GlideRecord('x_snagentic_source_tombstone');
        this._assertTable(tombstone);
        tombstone.initialize();
        this.security.canWriteRecord(tombstone, 'create');
        tombstone.setValue('artifact_type', change.artifact_type);
        tombstone.setValue('record_sys_id', change.sys_id);
        tombstone.setValue('record_name', current.name);
        tombstone.setValue('domain_id', current.domain.sys_id);
        tombstone.setValue('scope_id', current.application_scope.sys_id);
        tombstone.setValue('prior_sys_mod_count', current.sys_mod_count);
        tombstone.setValue('prior_revision', current.revision);
        tombstone.setValue('prior_hash', current.hash);
        tombstone.setValue('deleted_at', new GlideDateTime());
        tombstone.setValue('deleted_by', gs.getUserName());
        tombstone.setValue('correlation_id', correlationId);
        tombstone.setValue('state', 'pending');
        var tombstoneId = tombstone.insert();
        if (!tombstoneId) {
            throw this.security.error('tombstone_failed', 'Deletion was stopped because its tombstone could not be created', 500);
        }
        return String(tombstoneId);
    },

    _completeTombstone: function(tombstoneId) {
        var tombstone = new GlideRecord('x_snagentic_source_tombstone');
        this._assertTable(tombstone);
        if (!tombstone.get(tombstoneId)) {
            throw this.security.error('tombstone_finalize_failed', 'Deletion tombstone could not be finalized', 500);
        }
        this.security.canWriteRecord(tombstone, 'update');
        if (!tombstone.getElement('state').canWrite()) {
            throw this.security.error('field_access_denied', 'Tombstone state is not writable', 403);
        }
        tombstone.setValue('state', 'complete');
        if (!tombstone.update()) {
            throw this.security.error('tombstone_finalize_failed', 'Deletion tombstone could not be finalized', 500);
        }
    },

    _serialize: function(gr, type, config, includeValues) {
        this.security.canReadRecord(gr);
        var context = this._recordContext(gr);
        var values = {};
        for (var i = 0; i < config.readableFields.length; i++) {
            var field = config.readableFields[i];
            this.security.assertAllowedField(config, field, false);
            if (!gr.isValidField(field) || !gr.getElement(field).canRead()) {
                throw this.security.error('field_access_denied', 'An allowlisted field is unavailable or unreadable', 403, [field]);
            }
            var value = this.security.redactText(gr.getValue(field));
            if (value !== null && String(value).length > this.constants.MAX_FIELD_CHARS) {
                throw this.security.error(
                    'artifact_field_too_large',
                    'An allowlisted artifact field exceeds the export hard limit',
                    413,
                    [field]
                );
            }
            values[field] = value;
        }
        var modCount = parseInt(gr.getValue('sys_mod_count') || '0', 10);
        var updatedOn = gr.getValue('sys_updated_on') || '';
        var revision = updatedOn + ':' + String(modCount);
        var digest = this.hash.sha256({
            artifact_type: type,
            sys_id: gr.getUniqueValue(),
            domain: context.domain,
            application_scope: context.application_scope,
            values: values
        });
        var output = {
            artifact_type: type,
            sys_id: gr.getUniqueValue(),
            name: gr.getValue(config.nameField) || '',
            domain: context.domain,
            application_scope: context.application_scope,
            sys_mod_count: modCount,
            revision: revision,
            hash: digest
        };
        if (includeValues) {
            output.values = values;
        }
        return output;
    },

    _recordContext: function(gr) {
        var domain = {sys_id: 'global', name: 'global', path: '/'};
        if (gr.isValidField('sys_domain') &&
                gr.getValue('sys_domain') &&
                gr.getValue('sys_domain') !== 'global') {
            domain = {
                sys_id: gr.getValue('sys_domain'),
                name: gr.getDisplayValue('sys_domain') || '',
                path: ''
            };
            var domainRecord = gr.getElement('sys_domain').getRefRecord();
            if (domainRecord && domainRecord.isValidRecord() && domainRecord.isValidField('sys_domain_path')) {
                domain.path = domainRecord.getValue('sys_domain_path') || '';
            }
        }
        if (!gr.isValidField('sys_scope') || !gr.getValue('sys_scope')) {
            throw this.security.error('missing_record_scope', 'Artifact has no explicit application scope', 409);
        }
        var scope = {
            sys_id: gr.getValue('sys_scope'),
            name: '',
            scope: ''
        };
        var scopeRecord = gr.getElement('sys_scope').getRefRecord();
        if (scopeRecord && scopeRecord.isValidRecord()) {
            scope.name = scopeRecord.getValue('name') || '';
            scope.scope = scopeRecord.getValue('scope') || '';
        }
        return {domain: domain, application_scope: scope};
    },

    _collectContexts: function(config, contexts) {
        var table = new GlideRecord(config.table);
        this._assertTable(table);
        if (!table.isValidField('sys_domain') || !table.isValidField('sys_scope')) {
            return;
        }

        var aggregate = new GlideAggregate(config.table);
        aggregate.addNotNullQuery('sys_domain');
        aggregate.addNotNullQuery('sys_scope');
        aggregate.groupBy('sys_domain');
        aggregate.groupBy('sys_scope');
        aggregate.orderBy('sys_domain');
        aggregate.orderBy('sys_scope');
        aggregate.setLimit(this.constants.MAX_CONTEXT_GROUPS_PER_TYPE + 1);
        aggregate.query();

        var scanned = 0;
        while (aggregate.next()) {
            scanned++;
            if (scanned > this.constants.MAX_CONTEXT_GROUPS_PER_TYPE) {
                throw this.security.error(
                    'context_scan_limit_exceeded',
                    'Context discovery exceeded its configured safety limit',
                    413
                );
            }
            var domainId = String(aggregate.getValue('sys_domain') || '');
            var scopeId = String(aggregate.getValue('sys_scope') || '');
            if (!this.security.isValidRecordId(domainId) ||
                    !/^[0-9a-f]{32}$/i.test(scopeId)) {
                continue;
            }
            var key = domainId.toLowerCase() + ':' + scopeId.toLowerCase();
            if (contexts.hasOwnProperty(key)) {
                continue;
            }
            var context = this._authorizedExplicitContext(config.table, domainId, scopeId);
            if (context) {
                contexts[key] = context;
            }
        }
    },

    _authorizedExplicitContext: function(tableName, domainId, scopeId) {
        var gr = new GlideRecordSecure(tableName);
        gr.addQuery('sys_domain', domainId);
        gr.addQuery('sys_scope', scopeId);
        gr.setLimit(1);
        gr.query();
        if (!gr.next() || !gr.canRead() ||
                !gr.getElement('sys_domain').canRead() ||
                !gr.getElement('sys_scope').canRead()) {
            return null;
        }

        var explicitDomainId = String(gr.getValue('sys_domain') || '');
        var explicitScopeId = String(gr.getValue('sys_scope') || '');
        if (!explicitDomainId || !explicitScopeId ||
                explicitDomainId.toLowerCase() !== String(domainId).toLowerCase() ||
                explicitScopeId.toLowerCase() !== String(scopeId).toLowerCase()) {
            return null;
        }

        var domain;
        if (explicitDomainId === 'global') {
            domain = {sys_id: 'global', name: 'global', path: '/'};
        } else {
            var domainRecord = new GlideRecordSecure('sys_domain');
            if (!domainRecord.get(explicitDomainId) || !domainRecord.canRead()) {
                return null;
            }
            domain = {
                sys_id: explicitDomainId,
                name: domainRecord.getValue('name') || '',
                path: domainRecord.isValidField('sys_domain_path') ?
                    (domainRecord.getValue('sys_domain_path') || '') :
                    ''
            };
        }

        var scopeRecord = new GlideRecordSecure('sys_scope');
        if (!scopeRecord.get(explicitScopeId) || !scopeRecord.canRead()) {
            return null;
        }
        return {
            domain: domain,
            application_scope: {
                sys_id: explicitScopeId,
                name: scopeRecord.getValue('name') || '',
                scope: scopeRecord.getValue('scope') || ''
            }
        };
    },

    _addContextQuery: function(gr, domainId, scopeId) {
        if (!this.security.isValidRecordId(domainId) ||
                !/^[0-9a-f]{32}$/i.test(String(scopeId || ''))) {
            throw this.security.error('invalid_context', 'Domain or application scope is invalid', 400);
        }
        if (!gr.isValidField('sys_scope')) {
            throw this.security.error('table_scope_unsupported', 'Allowlisted table does not expose application scope', 501);
        }
        gr.addQuery('sys_scope', scopeId);
        if (gr.isValidField('sys_domain')) {
            gr.addQuery('sys_domain', domainId);
        } else if (domainId !== 'global') {
            throw this.security.error('domain_not_supported', 'This artifact type is restricted to the global domain', 400);
        }
    },

    _setContext: function(gr, domainId, scopeId) {
        if (!gr.isValidField('sys_scope')) {
            throw this.security.error('table_scope_unsupported', 'Allowlisted table does not expose application scope', 501);
        }
        if (!gr.getElement('sys_scope').canWrite()) {
            throw this.security.error('field_access_denied', 'Application scope is not writable', 403);
        }
        gr.setValue('sys_scope', scopeId);
        if (gr.isValidField('sys_domain')) {
            if (!gr.getElement('sys_domain').canWrite()) {
                throw this.security.error('field_access_denied', 'Domain is not writable', 403);
            }
            gr.setValue('sys_domain', domainId);
        } else if (domainId !== 'global') {
            throw this.security.error('domain_not_supported', 'This artifact type is restricted to the global domain', 400);
        }
    },

    _setValues: function(gr, config, values) {
        for (var field in values) {
            if (!values.hasOwnProperty(field)) {
                continue;
            }
            this.security.assertAllowedField(config, field, true);
            if (!gr.isValidField(field) || !gr.getElement(field).canWrite()) {
                throw this.security.error('field_access_denied', 'An allowlisted field is unavailable or unwritable', 403, [field]);
            }
            gr.setValue(field, values[field]);
        }
    },

    _revisionTimestamp: function(revision) {
        var value = String(revision || '');
        var separator = value.lastIndexOf(':');
        if (separator < 1) {
            throw this.security.error('invalid_concurrency', 'Revision is invalid', 400);
        }
        return value.substring(0, separator);
    },

    _assertTable: function(gr) {
        if (!gr.isValid()) {
            throw this.security.error('allowlisted_table_unavailable', 'An allowlisted application table is unavailable', 501);
        }
    },

    type: 'SnSourceRepository'
};
