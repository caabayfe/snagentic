(function process(request, response) {
    var security = new SnSourceSecurity();
    var correlationId = security.correlationId(request);
    var responder = new SnSourceResponder(response, correlationId);
    try {
        security.requireRole('export');
        var validator = new SnSourceValidator();
        var items = validator.validateExportRequest(request.body.data);
        var artifacts = new SnSourceRepository().exportArtifacts(items);
        responder.success({artifacts: artifacts}, {count: artifacts.length});
    } catch (error) {
        responder.failure(error);
    }
})(request, response);
