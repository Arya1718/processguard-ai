using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Builder;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.IdentityModel.Tokens;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using ProcessGuard.Middleware.Auth;
using ProcessGuard.Middleware.Clients;
using ProcessGuard.Middleware.Data;
using ProcessGuard.Middleware.Health;
using ProcessGuard.Middleware.Infrastructure;

var builder = WebApplication.CreateBuilder(args);

// ---------------------------------------------------------------------------
// Configuration (fail-fast). All settings come from environment variables.
// In staging/prod the same PGAI_* variables are backed by Azure Key Vault
// via managed identity instead of a .env file; nothing below changes.
// ---------------------------------------------------------------------------
var config = MiddlewareConfig.Load(); // throws ClearConfigurationException if anything required is missing
builder.Services.AddSingleton(config);

// Prompt 7: the role->capability policy table (external config, shared with
// the Python agent-service). Fails fast if missing/corrupt.
var rbac = RbacPolicy.Load(config.Rbac.PolicyFile);
builder.Services.AddSingleton(rbac);

// ---------------------------------------------------------------------------
// Structured JSON logging. Every line carries CorrelationId, Service,
// Timestamp and Level (enrichment is registered further below).
// ---------------------------------------------------------------------------
builder.Logging.ClearProviders();
builder.Logging.AddJsonConsole(options =>
{
    options.JsonWriterOptions = new System.Text.Json.JsonWriterOptions { Indented = false };
    options.UseUtcTimestamp = true;
    options.TimestampFormat = "O";
    options.IncludeScopes = true; // request scope carries CorrelationId + Service onto every line
});
builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
builder.Logging.AddFilter("Microsoft.EntityFrameworkCore.Database.Command", LogLevel.Information);

// ---------------------------------------------------------------------------
// Prompt 9: OpenTelemetry metrics + tracing (stand-in for Application Insights).
// Metrics: ASP.NET Core built-ins + prometheus-net scrape endpoint at /metrics.
// Traces: OTLP exporter to the collector (http://otel-collector:4317 default).
// ---------------------------------------------------------------------------
builder.Services.AddOpenTelemetry()
    .ConfigureResource(resource => resource
        .AddService(serviceName: "processguard-middleware", serviceVersion: "1.0.0"))
    .WithMetrics(metrics =>
    {
        metrics.AddAspNetCoreInstrumentation()
               .AddHttpClientInstrumentation()
               .AddRuntimeInstrumentation()
               .AddMeter("ProcessGuard.Middleware");
        // Prometheus scrape endpoint
        metrics.AddPrometheusExporter(options =>
        {
            options.ScrapeEndpointPath = "/metrics";
            options.ScrapeResponseCacheDurationMilliseconds = 0;
        });
    })
    .WithTracing(tracing =>
    {
        tracing.AddAspNetCoreInstrumentation()
               .AddHttpClientInstrumentation()
               .AddSource("ProcessGuard.Middleware");
        var otelEndpoint = Environment.GetEnvironmentVariable("OTEL_EXPORTER_OTLP_ENDPOINT")
            ?? "http://otel-collector:4317";
        tracing.AddOtlpExporter(opts =>
        {
            opts.Endpoint = new Uri(otelEndpoint);
        });
    });

// ---------------------------------------------------------------------------
// Correlation ID middleware: honors X-Correlation-Id or mints a new GUID,
// exposes it via IHttpContextAccessor, and echoes it on every response.
// ---------------------------------------------------------------------------
builder.Services.AddHttpContextAccessor();
builder.Services.AddScoped<ICorrelationContext, CorrelationContext>();
// NOTE: CorrelationIdMiddleware is NOT registered in DI. UseMiddleware<>
// supplies RequestDelegate itself and resolves MiddlewareConfig from the
// container; registering the class would fail Development-mode constructor
// validation (RequestDelegate is not a container-resolvable service).

// ---------------------------------------------------------------------------
// EF Core + Npgsql (local Postgres standing in for Azure SQL).
// ---------------------------------------------------------------------------
builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(config.ConnectionStrings.Postgres));

// ---------------------------------------------------------------------------
// Health checks: readiness genuinely pings Postgres and Redis.
// ---------------------------------------------------------------------------
builder.Services.AddHealthChecks()
    .AddCheck<PostgresHealthCheck>("postgres", tags: ["ready"])
    .AddCheck<RedisHealthCheck>("redis", tags: ["ready"]);

// ---------------------------------------------------------------------------
// Prompt 7: REAL OIDC bearer authentication. Tokens issued by the identity
// provider (local OIDC stand-in now; Microsoft Entra ID later) are validated
// with the standard JWT bearer handler: signature via the provider's JWKS
// (fetched from the authority's discovery metadata), issuer, audience and
// lifetime. No hand-rolled validation anywhere.
//
// MapInboundClaims=false keeps the token's original claim names (roles,
// site_id, preferred_username) -- the RBAC layer and site scoping read those
// directly (docs/auth-flow.md documents the claim structure).
// ---------------------------------------------------------------------------
builder.Services
    .AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(options =>
    {
        options.Authority = config.Oidc.Authority;
        options.RequireHttpsMetadata = config.Oidc.RequireHttpsMetadata;
        options.MapInboundClaims = false;
        options.TokenValidationParameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuers = config.Oidc.ValidIssuers,
            ValidateAudience = true,
            ValidAudience = config.Oidc.Audience,
            ValidateIssuerSigningKey = true,
            ValidateLifetime = true,
            ClockSkew = TimeSpan.FromSeconds(30),
            NameClaimType = "preferred_username",
            RoleClaimType = "roles",
        };
    });

// Prompt 7: policy-based authorization from the external RBAC table.
builder.Services.AddAuthorization(options => RbacPolicies.Register(options, rbac));

// Per-request site scope: every incident query is forced through the caller's
// site_id claim (query-layer enforcement, not route-layer).
builder.Services.AddScoped<SiteScope>(sp =>
{
    var http = sp.GetRequiredService<IHttpContextAccessor>().HttpContext
               ?? throw new InvalidOperationException("no HTTP context");
    return SiteScope.FromUser(http.User);
});

// ---------------------------------------------------------------------------
// Typed HTTP client for the Python agent service (correlation ID forwarded).
// ---------------------------------------------------------------------------
builder.Services.AddHttpClient<IAgentServiceClient, AgentServiceClient>(client =>
{
    client.BaseAddress = new Uri(config.AgentService.BaseUrl);
    client.Timeout = TimeSpan.FromSeconds(30);
});

builder.Services.AddControllers(options =>
{
    // Cross-site access attempts render as a clear 403.
    options.Filters.Add<CrossSiteAccessExceptionFilter>();
});
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen();

var app = builder.Build();

// Apply EF Core migrations automatically in Development so `docker compose up`
// works with zero manual steps. Production would run migrations as a job.
using (var scope = app.Services.CreateScope())
{
    var db = scope.ServiceProvider.GetRequiredService<AppDbContext>();
    db.Database.Migrate();
    await DemoSeeder.SeedAsync(db, app.Logger);
}

// Startup marker: after migrations, record that initialization completed.
// Health/startup reads this flag; before first run it reports NotInitialized.
StartupState.MarkStarted(app.Logger);

app.UseMiddleware<CorrelationIdMiddleware>();

// Prompt 9: expose Prometheus metrics scrape endpoint at /metrics.
// The OpenTelemetry metrics provider (configured above) feeds the scrape
// endpoint; AddAspNetCoreInstrumentation already adds server spans + HTTP
// request/latency metrics for every incoming request.
app.MapPrometheusScrapingEndpoint();

if (app.Environment.IsDevelopment())
{
    app.UseSwagger();
    app.UseSwaggerUI();
}

app.UseAuthentication();
app.UseAuthorization();

app.MapControllers();

// Health endpoints are open (no auth) so orchestrators can probe them.
app.MapHealthEndpoints();

app.Run();

// Expose the implicit Program class for integration tests.
public partial class Program { }
