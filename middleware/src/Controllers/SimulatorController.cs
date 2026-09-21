using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using ProcessGuard.Middleware.Auth;
using ProcessGuard.Middleware.Clients;

namespace ProcessGuard.Middleware.Controllers;

/// <summary>
/// DEV/DEMO simulator controls. Policy-gated by the RBAC table
/// (MaintenanceEngineer + PlantManager): an Operator cannot ramp sensors.
/// The scenario itself targets the demo equipment on the caller's own site.
/// </summary>
[ApiController]
[Route("api/v1/simulator")]
[Authorize(Policy = RbacPolicies.Simulator)]
public sealed class SimulatorController : ControllerBase
{
    private readonly IAgentServiceClient _agent;

    public SimulatorController(IAgentServiceClient agent) => _agent = agent;

    /// <summary>DEV/DEMO: ramps CP-04 sensors toward the reference incident over ~30-60s.</summary>
    [HttpPost("trigger-scenario/cooling-tower-incident")]
    public async Task<IActionResult> TriggerCoolingTower(CancellationToken ct)
    {
        var payload = await _agent.TriggerCoolingTowerScenarioAsync(ct);
        return Ok(payload);
    }

    /// <summary>DEV/DEMO: returns the simulator to normal-mode readings.</summary>
    [HttpPost("reset")]
    public async Task<IActionResult> Reset(CancellationToken ct)
    {
        var payload = await _agent.ResetSimulatorAsync(ct);
        return Ok(payload);
    }
}
