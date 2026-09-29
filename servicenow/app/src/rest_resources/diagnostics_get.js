(function process(request, response) {
    var security = new SnSourceSecurity();
    var correlationId = security.correlationId(request);
    var responder = new SnSourceResponder(response, correlationId);
    try {
        security.requireRole('diagnostics');
        responder.success(new SnSourceDiagnostics().collect());
    } catch (error) {
        responder.failure(error);
    }
})(request, response);
