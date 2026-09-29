(function process(request, response) {
    var security = new SnSourceSecurity();
    var correlationId = security.correlationId(request);
    var responder = new SnSourceResponder(response, correlationId);
    try {
        security.requireRole('export');
        var type = String(request.queryParams.artifact_type || '');
        var domainId = String(request.queryParams.domain_id || '');
        var scopeId = String(request.queryParams.scope_id || '');
        security.artifactConfig(type);
        security.requireContext({
            domain: {sys_id: domainId},
            application_scope: {sys_id: scopeId}
        });
        var limit = security.pageLimit(request.queryParams.limit);
        var cursor = security.validateCursor(request.queryParams.cursor);
        var result = new SnSourceRepository().listArtifacts(
            type,
            domainId,
            scopeId,
            limit,
            cursor
        );
        responder.success(result, {
            artifact_type: type,
            domain_id: domainId,
            scope_id: scopeId,
            limit: limit
        });
    } catch (error) {
        responder.failure(error);
    }
})(request, response);
