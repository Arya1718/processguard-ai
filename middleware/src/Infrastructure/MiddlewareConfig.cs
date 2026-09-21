namespace ProcessGuard.Middleware.Infrastructure;

/// <summary>
/// Central configuration for the middleware, loaded from environment variables.
/// Every required variable must be present at startup — the service refuses to
/// start (fail fast) rather than run silently misconfigured.
///
/// AZURE NOTE: in staging/prod this same interface is backed by Azure Key
/// Vault via managed identity instead of a .env file. Code reads only
/// PGAI_* variables from the process environment, so swapping the source
/// of those variables does not change any of this code.
/// </summary>
public sealed class MiddlewareConfig
{
    public string ServiceName { get; init; } = "processguard-middleware";
    public string CorrelationHeader { get; init; } = "X-Correlation-Id";
    public OidcOptions Oidc { get; init; } = new();
    public RbacOptions Rbac { get; init; } = new();
    public ConnectionStringsOptions ConnectionStrings { get; init; } = new();
    public AgentServiceOptions AgentService { get; init; } = new();

    public static MiddlewareConfig Load()
    {
        Func<string, string?> env = Environment.GetEnvironmentVariable;

        string Required(string key)
        {
            var value = env(key);
            if (string.IsNullOrWhiteSpace(value))
            {
                throw new ClearConfigException(
                    $"Missing required environment variable '{key}'. " +
                    "Copy .env.example to .env at the repo root and try again. " +
                    "(In staging/prod these values come from Azure Key Vault via managed identity.)");
            }
            return value;
        }

        string Optional(string key, string fallback) =>
            env(key) is { Length: > 0 } v ? v : fallback;

        return new MiddlewareConfig
        {
            CorrelationHeader = Optional("PGAI_CORRELATION_HEADER", "X-Correlation-Id"),
            Oidc = new OidcOptions
            {
                // Authority is where the middleware FETCHES discovery metadata
                // (in-network URL); Issuer/InternalIssuer are the iss claims
                // considered valid on tokens. In the compose deployment both
                // are http://oidc-provider:8090; the browser reaches the same
                // provider through its host-published port.
                Authority = Required("PGAI_OIDC__AUTHORITY"),
                Audience = Required("PGAI_OIDC__AUDIENCE"),
                Issuer = Required("PGAI_OIDC__ISSUER"),
                InternalIssuer = Optional("PGAI_OIDC__INTERNALISSUER", string.Empty),
                RequireHttpsMetadata = Optional("PGAI_OIDC__REQUIREHTTPSMETADATA", "false") == "true",
            },
            Rbac = new RbacOptions
            {
                // Prompt 7: the role->capability table is EXTERNAL CONFIG
                // (same discipline as the HITL thresholds), shared verbatim
                // with the Python agent-service. Tune roles without a
                // code change; see docs/rbac-policy.md.
                PolicyFile = Optional("PGAI_RBAC__POLICYFILE", "config/rbac-policy.json"),
            },
            ConnectionStrings = new ConnectionStringsOptions
            {
                Postgres = Required("PGAI_CONNECTIONSTRINGS__POSTGRES"),
                Redis = Required("PGAI_CONNECTIONSTRINGS__REDIS"),
            },
            AgentService = new AgentServiceOptions
            {
                BaseUrl = Required("PGAI_AGENTSERVICE__BASEURL"),
            },
        };
    }
}

public sealed class OidcOptions
{
    public string Authority { get; init; } = string.Empty;
    public string Audience { get; init; } = string.Empty;
    public string Issuer { get; init; } = string.Empty;
    public string InternalIssuer { get; init; } = string.Empty;
    public bool RequireHttpsMetadata { get; init; }

    /// <summary>All iss values accepted on incoming tokens (host-facing and
    /// in-network views of the same identity provider).</summary>
    public IReadOnlyList<string> ValidIssuers =>
        string.IsNullOrEmpty(InternalIssuer)
            ? new[] { Issuer }
            : new[] { Issuer, InternalIssuer };
}

public sealed class RbacOptions
{
    public string PolicyFile { get; init; } = string.Empty;
}

public sealed class ConnectionStringsOptions
{
    public string Postgres { get; init; } = string.Empty;
    public string Redis { get; init; } = string.Empty;
}

public sealed class AgentServiceOptions
{
    public string BaseUrl { get; init; } = string.Empty;
}

/// <summary>Thrown when a required environment variable is missing at startup.</summary>
public sealed class ClearConfigException : Exception
{
    public ClearConfigException(string message) : base(message) { }
}
