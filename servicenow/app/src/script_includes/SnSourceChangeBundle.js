var SnSourceChangeBundle = Class.create();
SnSourceChangeBundle.prototype = {
    initialize: function() {
        this.security = new SnSourceSecurity();
        this.validator = new SnSourceValidator();
        this.repository = new SnSourceRepository();
    },

    preflight: function(body) {
        return this.validator.validateBundle(body, true);
    },

    apply: function(body, correlationId) {
        this.security.assertDevelopmentWrite();
        var validation = this.validator.validateBundle(body, true);
        var completed = [];
        for (var i = 0; i < body.changes.length; i++) {
            try {
                var artifact = this.repository.applyChange(body.changes[i], correlationId);
                completed.push({
                    index: i,
                    operation: body.changes[i].operation,
                    artifact_type: body.changes[i].artifact_type,
                    sys_id: artifact.sys_id,
                    domain: artifact.domain,
                    application_scope: artifact.application_scope,
                    revision: artifact.revision || null,
                    hash: artifact.hash || null,
                    tombstoned: artifact.tombstoned === true
                });
            } catch (error) {
                error.safeDetails = [{
                    failed_index: i,
                    completed_count: completed.length,
                    completed: this.security.sanitize(completed)
                }];
                throw error;
            }
        }
        gs.info(
            '[Snagentic Source Exchange] applied change bundle correlation_id={0} item_count={1}',
            correlationId,
            completed.length
        );
        return {
            bundle_id: validation.bundle_id,
            applied: true,
            completed: completed
        };
    },

    type: 'SnSourceChangeBundle'
};
