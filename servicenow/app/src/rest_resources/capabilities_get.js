(function process(request, response) {
    var security = new SnSourceSecurity();
    var correlationId = security.correlationId(request);
    var responder = new SnSourceResponder(response, correlationId);
    try {
        security.requireRole('export');
        var constants = new SnSourceConstants();
        responder.success({
            categories: constants.capabilityCategories,
            capabilities: constants.capabilities,
            artifact_types: constants.artifactTypeCapabilities()
        });
    } catch (error) {
        responder.failure(error);
    }
})(request, response);
