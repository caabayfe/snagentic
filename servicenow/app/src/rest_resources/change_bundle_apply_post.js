(function process(request, response) {
    var security = new SnSourceSecurity();
    var correlationId = security.correlationId(request);
    var responder = new SnSourceResponder(response, correlationId);
    try {
        security.requireRole('dev_import');
        var result = new SnSourceChangeBundle().apply(request.body.data, correlationId);
        responder.success(result);
    } catch (error) {
        responder.failure(error);
    }
})(request, response);
