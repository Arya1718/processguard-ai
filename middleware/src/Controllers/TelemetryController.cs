using System.Text.Json;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using ProcessGuard.Middleware.Clients;

namespace ProcessGuard.Middleware.Controllers;

[ApiController]
[Route("api/v1/telemetry")]
[Authorize] // every non-health, non-dev-token route requires a valid token
public sealed class TelemetryController : ControllerBase
{
    private readonly IAgentServiceClient _agent;

    public TelemetryController(IAgentServiceClient agent) => _agent = agent;

    /// <summary>
    /// Protected sample route proving: valid JWT -> middleware -> typed agent
    /// client -> Python service, with the correlation ID forwarded throughout.
    /// </summary>
    [HttpGet("agent-live")]
    public async Task<IActionResult> AgentLive(CancellationToken ct)
    {
        var status = await _agent.GetHealthAsync(ct);
        return Ok(new { agentStatus = status.Status });
    }
}
