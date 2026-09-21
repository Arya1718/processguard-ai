namespace ProcessGuard.Middleware.Infrastructure;

/// <summary>
/// Tracks whether startup initialization (EF Core migrations) has completed.
/// In Development the middleware migrates on boot; a real deployment would
/// run migrations as a separate job and set this from the same signal.
/// </summary>
public static class StartupState
{
    private static volatile bool _started;

    public static bool IsStarted => _started;

    public static void MarkStarted(ILogger logger)
    {
        _started = true;
        logger.LogInformation("Startup initialization complete (migrations applied)");
    }
}
