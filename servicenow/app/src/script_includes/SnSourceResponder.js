var SnSourceResponder = Class.create();
SnSourceResponder.prototype = {
    initialize: function(response, correlationId) {
        this.response = response;
        this.correlationId = correlationId;
        this.security = new SnSourceSecurity();
        this.response.setHeader('X-Correlation-ID', correlationId);
        this.response.setHeader('Cache-Control', 'no-store');
        this.response.setHeader('Content-Type', 'application/json');
    },

    success: function(data, meta, status) {
        this.response.setStatus(status || 200);
        this.response.setBody(this.security.sanitize({
            ok: true,
            correlation_id: this.correlationId,
            data: data || {},
            meta: meta || {}
        }));
    },

    failure: function(error) {
        var status = error && error.safeStatus ? error.safeStatus : 500;
        var code = error && error.safeCode ? error.safeCode : 'internal_error';
        var message = error && error.safeCode ? error.message : 'The request could not be completed';
        var details = error && error.safeDetails ? error.safeDetails : [];
        this.response.setStatus(status);
        this.response.setBody(this.security.sanitize({
            ok: false,
            correlation_id: this.correlationId,
            error: {
                code: code,
                message: message,
                details: details
            }
        }));
    },

    type: 'SnSourceResponder'
};
