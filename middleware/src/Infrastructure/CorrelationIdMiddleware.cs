namespace ProcessGuard.Middleware.Infrastructure;

/// <summary>
/// Assigns a correlation ID to every incoming request (honoring an existing
/// X-Correlation-Id header), exposes it for the request lifetime, echoes it
/// on the response, and opens a logging scope so every structured log line
/// in this request carries CorrelationId + Service.
/// </summary>
public sealed class CorrelationIdMiddleware
{
    public const string ItemKey = "PGAI_CorrelationId";
    public const string DefaultHeaderName = "X-Correlation-Id";

    private readonly RequestDelegate _next;
    private readonly string _headerName;

    public CorrelationIdMiddleware(RequestDelegate next, MiddlewareConfig config)
    {
        _next = next;
        _headerName = config.CorrelationHeader;
    }

    public async Task InvokeAsync(HttpContext context, ILogger<CorrelationIdMiddleware> logger)
    {
        var correlationId =
            context.Request.Headers.TryGetValue(_headerName, out var existing) &&
            !string.IsNullOrWhiteSpace(existing) &&
            existing.ToString().Length <= 128
                ? existing.ToString().Trim()
                : Guid.NewGuid().ToString("N");

        context.Items[ItemKey] = correlationId;
        context.Response.Headers[_headerName] = correlationId;

        using (logger.BeginScope(new Dictionary<string, object>
               {
                   ["CorrelationId"] = correlationId,
                   ["Service"] = "processguard-middleware",
               }))
        {
            logger.LogInformation("Request started {Method} {Path}", context.Request.Method, context.Request.Path);
            await _next(context);
            logger.LogInformation("Request finished {Method} {Path} with status {StatusCode}",
                context.Request.Method, context.Request.Path, context.Response.StatusCode);
            // Serialize correlation state into standard scope; HttpLogging of body is not enabled by design.
        }
    }
}
