(function process(request, response) {
    var security = new SnSourceSecurity();
    var correlationId = security.correlationId(request);
    var responder = new SnSourceResponder(response, correlationId);
    try {
        security.requireRole('export');
        var limit = security.pageLimit(request.queryParams.limit);
        var cursor = security.validateContextCursor(request.queryParams.cursor);
        var result = new SnSourceRepository().listContexts(limit, cursor);
        responder.success(result, {limit: limit});
    } catch (error) {
        responder.failure(error);
    }
})(request, response);
