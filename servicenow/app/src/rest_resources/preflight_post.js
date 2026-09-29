(function process(request, response) {
    var security = new SnSourceSecurity();
    var correlationId = security.correlationId(request);
    var responder = new SnSourceResponder(response, correlationId);
    try {
        security.requireRole('validate');
        var result = new SnSourceChangeBundle().preflight(request.body.data);
        responder.success(result);
    } catch (error) {
        responder.failure(error);
    }
})(request, response);
