namespace ProcessGuard.Middleware.Infrastructure;

/// <summary>
/// Correlation context for the current request. Middleware sets it;
/// everything else (logging scopes, downstream HTTP calls) reads it.
/// </summary>
public interface ICorrelationContext
{
    string CorrelationId { get; }
}

public sealed class CorrelationContext : ICorrelationContext
{
    private readonly IHttpContextAccessor _accessor;

    public CorrelationContext(IHttpContextAccessor accessor) => _accessor = accessor;

    public string CorrelationId =>
        _accessor.HttpContext?.Items[CorrelationIdMiddleware.ItemKey] as string ?? "no-correlation-id";
}
