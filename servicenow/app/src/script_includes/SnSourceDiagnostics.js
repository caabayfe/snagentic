var SnSourceDiagnostics = Class.create();
SnSourceDiagnostics.prototype = {
    initialize: function() {
        this.constants = new SnSourceConstants();
        this.security = new SnSourceSecurity();
    },

    collect: function() {
        var configuredWindow = parseInt(
            gs.getProperty('x_snagentic_source.diagnostic_window_minutes', '15'),
            10
        );
        if (isNaN(configuredWindow) || configuredWindow < 1) {
            configuredWindow = 15;
        }
        configuredWindow = Math.min(configuredWindow, this.constants.MAX_DIAGNOSTIC_MINUTES);
        var environment = String(
            gs.getProperty('x_snagentic_source.environment', 'production')
        ).toLowerCase();
        var types = [];
        for (var key in this.constants.artifactTypes) {
            if (this.constants.artifactTypes.hasOwnProperty(key)) {
                types.push(key);
            }
        }
        types.sort();
        return {
            api_version: this.constants.API_VERSION,
            environment: environment,
            write_gate_open: this._writeGateOpen(),
            limits: {
                max_page_size: this.constants.MAX_PAGE_SIZE,
                max_context_groups_per_type: this.constants.MAX_CONTEXT_GROUPS_PER_TYPE,
                max_export_items: this.constants.MAX_EXPORT_ITEMS,
                max_change_items: this.constants.MAX_CHANGE_ITEMS,
                max_body_chars: this.constants.MAX_BODY_CHARS
            },
            allowlisted_artifact_types: types,
            window_minutes: configuredWindow,
            aggregate: {
                tombstones_created: this._tombstoneCount(configuredWindow)
            },
            redaction: {
                credentials: true,
                encrypted_properties: true,
                journals: true,
                request_bodies: true,
                attachments: true,
                business_records: true
            }
        };
    },

    _writeGateOpen: function() {
        try {
            this.security.assertDevelopmentWrite();
            return true;
        } catch (ignore) {
            return false;
        }
    },

    _tombstoneCount: function(minutes) {
        var aggregate = new GlideAggregate('x_snagentic_source_tombstone');
        if (!aggregate.isValid()) {
            return 0;
        }
        aggregate.addQuery('state', 'complete');
        aggregate.addQuery('deleted_at', '>=', gs.minutesAgoStart(minutes));
        aggregate.addAggregate('COUNT');
        aggregate.query();
        if (aggregate.next()) {
            return parseInt(aggregate.getAggregate('COUNT'), 10) || 0;
        }
        return 0;
    },

    type: 'SnSourceDiagnostics'
};
