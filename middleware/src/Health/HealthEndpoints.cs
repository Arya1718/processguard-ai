using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Diagnostics.HealthChecks;
using ProcessGuard.Middleware.Data;
using ProcessGuard.Middleware.Infrastructure;

namespace ProcessGuard.Middleware.Health;

/// <summary>
/// The three health endpoints required on every ProcessGuard service:
///   live    -> shallow liveness probe, no dependencies touched
///   ready   -> genuinely pings Postgres and Redis, per-dependency status
///   startup -> confirms migrations/init have completed
/// All are unauthenticated so orchestrators can probe them.
/// </summary>
public static class HealthEndpoints
{
    public static IEndpointRouteBuilder MapHealthEndpoints(this IEndpointRouteBuilder app)
    {
        app.MapGet("/api/v1/health/live", () => Results.Ok(new { status = "ok" }))
           .AllowAnonymous();

        app.MapGet("/api/v1/health/ready", async (AppDbContext db, MiddlewareConfig config, CancellationToken ct) =>
        {
            var postgres = await CheckAsync(async () =>
            {
                if (!await db.Database.CanConnectAsync(ct))
                {
                    throw new InvalidOperationException("postgres cannot connect");
                }
                return "postgres reachable";
            });

            var redis = await CheckAsync(async () =>
            {
                var redisCheck = new RedisHealthCheck(config);
                var result = await redisCheck.CheckHealthAsync(new HealthCheckContext());
                if (result.Status != HealthStatus.Healthy) throw new InvalidOperationException(result.Description);
                return result.Description ?? "redis reachable";
            });

            var ready = postgres == "ok" && redis == "ok";
            return Results.Json(
                new { status = ready ? "ready" : "degraded", dependencies = new { postgres, redis } },
                statusCode: ready ? 200 : 503);
        })
           .AllowAnonymous();

        app.MapGet("/api/v1/health/startup", () => Results.Json(new
        {
            status = StartupState.IsStarted ? "started" : "not_started",
            migrations = StartupState.IsStarted ? "applied" : "pending",
        }, statusCode: StartupState.IsStarted ? 200 : 503))
           .AllowAnonymous();

        return app;
    }

    private static async Task<string> CheckAsync(Func<Task<string>> probe)
    {
        try
        {
            await probe();
            return "ok";
        }
        catch (Exception)
        {
            return "failed";
        }
    }
}
