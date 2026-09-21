using Microsoft.Extensions.Diagnostics.HealthChecks;
using Npgsql;
using ProcessGuard.Middleware.Infrastructure;
using StackExchange.Redis;

namespace ProcessGuard.Middleware.Health;

/// <summary>Genuinely pings Postgres (stand-in for Azure SQL). Used by /health/ready.</summary>
public sealed class PostgresHealthCheck : IHealthCheck
{
    private readonly string _connectionString;

    public PostgresHealthCheck(MiddlewareConfig config) => _connectionString = config.ConnectionStrings.Postgres;

    public async Task<HealthCheckResult> CheckHealthAsync(
        HealthCheckContext context, CancellationToken cancellationToken = default)
    {
        try
        {
            await using var conn = new NpgsqlConnection(_connectionString);
            await conn.OpenAsync(cancellationToken);
            await using var cmd = conn.CreateCommand();
            cmd.CommandText = "SELECT 1";
            await cmd.ExecuteScalarAsync(cancellationToken);
            return HealthCheckResult.Healthy("postgres reachable");
        }
        catch (Exception ex)
        {
            return HealthCheckResult.Unhealthy("postgres unreachable", ex);
        }
    }
}

/// <summary>Genuinely pings Redis (cache + pub/sub stand-in). Used by /health/ready.</summary>
public sealed class RedisHealthCheck : IHealthCheck
{
    private readonly string _endpoint;

    public RedisHealthCheck(MiddlewareConfig config) => _endpoint = config.ConnectionStrings.Redis;

    public async Task<HealthCheckResult> CheckHealthAsync(
        HealthCheckContext context, CancellationToken cancellationToken = default)
    {
        try
        {
            // Short-lived multiplexer per probe: readiness must reflect the
            // dependency's current state, not a cached connection.
            using var redis = await ConnectionMultiplexer.ConnectAsync(
                new ConfigurationOptions
                {
                    EndPoints = { _endpoint },
                    ConnectTimeout = 2000,
                    SyncTimeout = 2000,
                    AbortOnConnectFail = false,
                });
            var pong = await redis.GetDatabase().PingAsync();
            return HealthCheckResult.Healthy($"redis reachable (ping {pong.TotalMilliseconds:F0}ms)");
        }
        catch (Exception ex)
        {
            return HealthCheckResult.Unhealthy("redis unreachable", ex);
        }
    }
}
