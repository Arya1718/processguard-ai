using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using ProcessGuard.Middleware.Auth;
using ProcessGuard.Middleware.Clients;
using ProcessGuard.Middleware.Data;
using Microsoft.EntityFrameworkCore;

namespace ProcessGuard.Middleware.Controllers;

/// <summary>
/// Passthrough to the agent service. The .NET middleware remains the single
/// entry point: the frontend never calls the Python service directly.
///
/// Prompt 7 security model on every route:
///   * Authentication: a valid OIDC bearer token (validated against the
///     identity provider's JWKS).
///   * SITE SCOPING at the QUERY layer: the requested site is clamped to the
///     token's site_id claim, and single-incident fetches verify the
///     incident's SiteId against the caller's scope BEFORE proxying -- a
///     Site 07 user cannot read a Site 12 incident by guessing its id.
///   * Capability policies from the external RBAC table (Operator cannot
///     decide; MaintenanceEngineer caps at HITL level 2; PlantManager at 3).
/// </summary>
[ApiController]
[Route("api/v1")]
[Authorize]
public sealed class IncidentsController : ControllerBase
{
    public sealed record DecisionRequest(string? DecidedBy, string? Justification);
    public sealed record ResolveRequest(string? ResolvedBy, string? Note);
    public sealed record AskRequest(string? Question);

    private readonly IAgentServiceClient _agent;
    private readonly SiteScope _scope;
    private readonly AppDbContext _db;

    public IncidentsController(IAgentServiceClient agent, SiteScope scope, AppDbContext db)
    {
        _agent = agent;
        _scope = scope;
        _db = db;
    }

    /// <summary>Paginated incident list, filterable by status. The site filter
    /// is FORCED to the caller's site (an explicit ?siteId= for another site
    /// is a 403, not a silent rewrite).</summary>
    [HttpGet("incidents")]
    [Authorize(Policy = RbacPolicies.IncidentRead)]
    public async Task<IActionResult> List(
        [FromQuery] string? status,
        [FromQuery] string? siteId,
        [FromQuery] int limit = 50,
        [FromQuery] int offset = 0,
        CancellationToken ct = default)
    {
        var scopedSiteId = _scope.ResolveSiteId(siteId);
        var result = await _agent.ListIncidentsAsync(status, scopedSiteId, limit, offset, ct);
        return Ok(result);
    }

    /// <summary>Single incident detail including the triggering sensor evidence.
    /// The incident's site is checked against the caller's scope FIRST.</summary>
    [HttpGet("incidents/{id}")]
    [Authorize(Policy = RbacPolicies.IncidentRead)]
    public async Task<IActionResult> Get(string id, CancellationToken ct)
    {
        await EnsureIncidentInScopeAsync(id, ct);
        var incident = await _agent.GetIncidentAsync(id, ct);
        return incident is null ? NotFound(new { error = "incident not found" }) : Ok(incident);
    }

    /// <summary>Knowledge Agent output: retrieved SOP chunks + matched historical incidents.</summary>
    [HttpGet("incidents/{id}/evidence")]
    [Authorize(Policy = RbacPolicies.IncidentRead)]
    public async Task<IActionResult> Evidence(string id, CancellationToken ct)
    {
        await EnsureIncidentInScopeAsync(id, ct);
        var payload = await _agent.GetIncidentEvidenceAsync(id, ct);
        return payload is null ? NotFound(new { error = "incident not found" }) : Ok(payload);
    }

    /// <summary>Root-Cause Agent output: hypothesis, confidence, cited evidence.</summary>
    [HttpGet("incidents/{id}/root-cause")]
    [Authorize(Policy = RbacPolicies.IncidentRead)]
    public async Task<IActionResult> RootCause(string id, CancellationToken ct)
    {
        await EnsureIncidentInScopeAsync(id, ct);
        var payload = await _agent.GetIncidentRootCauseAsync(id, ct);
        return payload is null ? NotFound(new { error = "incident not found" }) : Ok(payload);
    }

    /// <summary>Risk Agent output: severity, consequences, deterministic HITL level.</summary>
    [HttpGet("incidents/{id}/risk")]
    [Authorize(Policy = RbacPolicies.IncidentRead)]
    public async Task<IActionResult> Risk(string id, CancellationToken ct)
    {
        await EnsureIncidentInScopeAsync(id, ct);
        var payload = await _agent.GetIncidentRiskAsync(id, ct);
        return payload is null ? NotFound(new { error = "incident not found" }) : Ok(payload);
    }

    /// <summary>Recommendation Agent output: actions, rationale, visible tool-call log.</summary>
    [HttpGet("incidents/{id}/recommendation")]
    [Authorize(Policy = RbacPolicies.IncidentRead)]
    public async Task<IActionResult> Recommendation(string id, CancellationToken ct)
    {
        await EnsureIncidentInScopeAsync(id, ct);
        var payload = await _agent.GetIncidentRecommendationAsync(id, ct);
        return payload is null ? NotFound(new { error = "incident not found" }) : Ok(payload);
    }

    /// <summary>HITL decision: approve the pending recommendation. BOTH the
    /// site scope AND the role-to-HITL-level policy must pass; the approver
    /// identity comes from the validated token, never the body.</summary>
    [HttpPost("incidents/{id}/approve")]
    public async Task<IActionResult> Approve(string id, [FromBody] DecisionRequest request, CancellationToken ct)
        => await _decision(id, request, decision: "approved", ct);

    /// <summary>HITL decision: reject the pending recommendation. Justification
    /// required at level 3 (enforced again downstream).</summary>
    [HttpPost("incidents/{id}/reject")]
    public async Task<IActionResult> Reject(string id, [FromBody] DecisionRequest request, CancellationToken ct)
        => await _decision(id, request, decision: "rejected", ct);

    /// <summary>Grounded operator Q&A for one incident (Prompt 6, the "Why?"
    /// panel). The question is answered ONLY from the incident's recorded
    /// root cause + evidence + risk + recommendation; the agent service
    /// deterministically rejects any answer whose claims are not backed by
    /// that record. No new agent -- explanation, not planning.</summary>
    [HttpPost("incidents/{id}/ask")]
    [Authorize(Policy = RbacPolicies.Chat)]
    public async Task<IActionResult> Ask(string id, [FromBody] AskRequest request, CancellationToken ct)
    {
        if (request is null || string.IsNullOrWhiteSpace(request.Question))
        {
            return BadRequest(new { error = "question is required" });
        }

        await EnsureIncidentInScopeAsync(id, ct);
        try
        {
            var payload = await _agent.AskAboutIncidentAsync(id, new { question = request.Question }, ct);
            return payload is null ? NotFound(new { error = "incident not found" }) : Ok(payload);
        }
        catch (AgentServiceException ex)
        {
            object? detail = null;
            try
            {
                detail = System.Text.Json.JsonSerializer.Deserialize<System.Text.Json.JsonElement>(ex.Body);
            }
            catch
            {
                // not JSON — fall through to the generic payload
            }
            return StatusCode((int)ex.StatusCode, detail ?? new { error = ex.Message });
        }
    }

    /// <summary>Action Agent output (Prompt 5): what was executed on the mock
    /// ERP/CMMS, the CMMS references, per-operation outcomes, and status.
    /// Read passthrough only -- the middleware has NO route to the CMMS
    /// itself; all external interaction is mediated by the Action Agent.</summary>
    [HttpGet("incidents/{id}/action")]
    [Authorize(Policy = RbacPolicies.IncidentRead)]
    public async Task<IActionResult> Action(string id, CancellationToken ct)
    {
        await EnsureIncidentInScopeAsync(id, ct);
        var payload = await _agent.GetIncidentActionAsync(id, ct);
        return payload is null ? NotFound(new { error = "incident not found" }) : Ok(payload);
    }

    /// <summary>Manual resolution (Prompt 5): for incidents with no CMMS work
    /// order, or as an operator override. Resolver identity comes from the
    /// token; requires a role with resolve capability (RBAC table).</summary>
    [HttpPost("incidents/{id}/resolve")]
    [Authorize(Policy = RbacPolicies.Resolve)]
    public async Task<IActionResult> Resolve(string id, [FromBody] ResolveRequest request, CancellationToken ct)
    {
        await EnsureIncidentInScopeAsync(id, ct);
        var resolvedBy = User.DisplayName();
        if (string.IsNullOrWhiteSpace(resolvedBy))
        {
            return BadRequest(new { error = "no authenticated identity on token; cannot record a resolution" });
        }

        var body = new
        {
            resolved_by = resolvedBy,
            note = request?.Note,
        };

        try
        {
            var payload = await _agent.ResolveIncidentAsync(id, body, ct);
            return payload is null ? NotFound(new { error = "incident not found" }) : Ok(payload);
        }
        catch (AgentServiceException ex)
        {
            object? detail = null;
            try
            {
                detail = System.Text.Json.JsonSerializer.Deserialize<System.Text.Json.JsonElement>(ex.Body);
            }
            catch
            {
                // not JSON — fall through to the generic payload
            }
            return StatusCode((int)ex.StatusCode, detail ?? new { error = ex.Message });
        }
    }

    /// <summary>Full ordered state-machine transition history (audit trail / demo narrative).</summary>
    [HttpGet("incidents/{id}/state-history")]
    [Authorize(Policy = RbacPolicies.IncidentRead)]
    public async Task<IActionResult> StateHistory(string id, CancellationToken ct)
    {
        await EnsureIncidentInScopeAsync(id, ct);
        var payload = await _agent.GetIncidentStateHistoryAsync(id, ct);
        return payload is null ? NotFound(new { error = "incident not found" }) : Ok(payload);
    }

    /// <summary>Latest reading per sensor for a site (dashboard poll endpoint).
    /// Site-scoped like every other read.</summary>
    [HttpGet("sites/{siteId}/sensors/latest")]
    [Authorize(Policy = RbacPolicies.IncidentRead)]
    public async Task<IActionResult> LatestReadings(string siteId, CancellationToken ct)
    {
        var scopedSiteId = _scope.ResolveSiteId(siteId);
        var payload = await _agent.GetLatestSensorReadingsAsync(scopedSiteId, ct);
        return payload is null ? NotFound(new { error = "site not found" }) : Ok(payload);
    }

    /// <summary>
    /// Query-layer site enforcement: BEFORE any proxying, the incident's
    /// SiteId is read from OUR database and compared with the caller's
    /// site_id claim. A cross-site id (guessed or enumerated) yields 403 --
    /// not a silent filter and not a 404 (the prompt explicitly requires a
    /// policy refusal for cross-site attempts).
    /// </summary>
    private async Task EnsureIncidentInScopeAsync(string id, CancellationToken ct)
    {
        if (!Guid.TryParse(id, out var incidentId))
        {
            return; // malformed ids fall through to the downstream 404
        }
        var siteId = await _db.Incidents
            .Where(i => i.Id == incidentId)
            .Select(i => (Guid?)i.SiteId)
            .FirstOrDefaultAsync(ct);
        if (siteId.HasValue &&
            !string.Equals(siteId.Value.ToString(), _scope.SiteId, StringComparison.OrdinalIgnoreCase))
        {
            throw new CrossSiteAccessException(
                "this incident belongs to another site and is outside your access scope");
        }
    }

    private async Task<IActionResult> _decision(
        string id, DecisionRequest? request, string decision, CancellationToken ct)
    {
        await EnsureIncidentInScopeAsync(id, ct);

        // Query the incident's required HITL level from OUR database and
        // check it against the RBAC policy table: an Operator (cap 0) gets
        // 403 before the call leaves the middleware; a MaintenanceEngineer
        // deciding a Level 3 incident gets 403 here as well. The Python
        // service re-checks this from the forwarded token (second layer).
        var incident = await _db.Incidents
            .Where(i => i.Id == Guid.Parse(id))
            .Select(i => new { i.HitlLevel, i.RequiredApprovalLevel })
            .FirstOrDefaultAsync(ct);
        if (incident is null)
        {
            return NotFound(new { error = "incident not found" });
        }
        var requiredLevel = incident.HitlLevel ?? incident.RequiredApprovalLevel ?? 2;
        var policy = HttpContext.RequestServices.GetRequiredService<RbacPolicy>();
        var role = RbacPolicies.Role(User);
        if (policy.MaxApprovalLevel(role) < requiredLevel)
        {
            return StatusCode(StatusCodes.Status403Forbidden, new
            {
                error = $"role '{role ?? "(none)"}' cannot decide a HITL level {requiredLevel} recommendation",
                requiredApprovalLevel = requiredLevel,
                yourRole = role,
            });
        }

        // Approver identity comes from the validated token (the demo
        // provider's name/preferred_username claims). The body cannot
        // override it.
        var decidedBy = User.DisplayName();
        if (string.IsNullOrWhiteSpace(decidedBy))
        {
            return BadRequest(new { error = "no authenticated identity on token; cannot record a decision" });
        }

        var body = new
        {
            decided_by = decidedBy,
            justification = request?.Justification,
        };

        try
        {
            // The Python routes are /approve and /reject (actions); the
            // decision VALUE is "approved"/"rejected".
            var action = decision == "approved" ? "approve" : "reject";
            var payload = await _agent.PostIncidentDecisionAsync(id, action, body, ct);
            return payload is null ? NotFound(new { error = "incident not found" }) : Ok(payload);
        }
        catch (AgentServiceException ex)
        {
            object? detail = null;
            try
            {
                detail = System.Text.Json.JsonSerializer.Deserialize<System.Text.Json.JsonElement>(ex.Body);
            }
            catch
            {
                // not JSON — fall through to the generic payload
            }
            return StatusCode((int)ex.StatusCode, detail ?? new { error = ex.Message });
        }
    }
}
