using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using ProcessGuard.Middleware.Infrastructure;

namespace ProcessGuard.Middleware.Clients;

/// <summary>
/// Typed HTTP client for the Python agent service. All middleware-to-agent
/// calls go through this wrapper — never scatter raw HttpClient usage.
/// Forwards the current correlation ID so one request is traceable across
/// both services end to end.
/// </summary>
public interface IAgentServiceClient
{
    Task<AgentHealthStatus> GetHealthAsync(CancellationToken ct = default);

    // Prompt 7: end-to-end token validation -- the agent service re-validates
    // a forwarded token (signature via the provider's JWKS, issuer, audience,
    // lifetime) so the middleware never makes a purely local trust decision.
    Task ValidateTokenAsync(string accessToken, CancellationToken ct = default);

    Task<IncidentListResult> ListIncidentsAsync(
        string? status = null, string? siteId = null, int limit = 50, int offset = 0, CancellationToken ct = default);
    Task<JsonElement?> GetIncidentAsync(string incidentId, CancellationToken ct = default);
    Task<JsonElement?> GetIncidentEvidenceAsync(string incidentId, CancellationToken ct = default);
    Task<JsonElement?> GetIncidentRootCauseAsync(string incidentId, CancellationToken ct = default);

    // Prompt 4: risk + recommendation outputs, HITL decisions, state history.
    Task<JsonElement?> GetIncidentRiskAsync(string incidentId, CancellationToken ct = default);
    Task<JsonElement?> GetIncidentRecommendationAsync(string incidentId, CancellationToken ct = default);
    Task<JsonElement?> GetIncidentStateHistoryAsync(string incidentId, CancellationToken ct = default);
    Task<JsonElement?> PostIncidentDecisionAsync(string incidentId, string action, object body, CancellationToken ct = default);

    // Prompt 5: the Action Agent's execution record + manual resolution.
    Task<JsonElement?> GetIncidentActionAsync(string incidentId, CancellationToken ct = default);
    Task<JsonElement?> ResolveIncidentAsync(string incidentId, object body, CancellationToken ct = default);

    // Prompt 6: grounded operator Q&A for ONE incident ("Why?" panel).
    Task<JsonElement?> AskAboutIncidentAsync(string incidentId, object body, CancellationToken ct = default);

    Task<JsonElement?> GetLatestSensorReadingsAsync(string siteId, CancellationToken ct = default);
    Task<JsonElement?> TriggerCoolingTowerScenarioAsync(CancellationToken ct = default);
    Task<JsonElement?> ResetSimulatorAsync(CancellationToken ct = default);
}

public sealed record AgentHealthStatus(string Status);

/// <summary>Agent service returned a non-success status; carries the raw body
/// so the controller can pass the real error detail (e.g. a 409/422 from the
/// HITL decision endpoints) through to the caller.</summary>
public sealed class AgentServiceException : HttpRequestException
{
    public string Body { get; }

    public AgentServiceException(System.Net.HttpStatusCode statusCode, string body)
        : base($"Agent service returned {(int)statusCode}", inner: null, statusCode)
    {
        Body = body;
    }
}

public sealed record IncidentDto(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("siteId")] string SiteId,
    [property: JsonPropertyName("equipmentId")] string EquipmentId,
    // Prompt 6: human-readable equipment name joined by the agent service
    // (kept through the typed round-trip so the alert feed shows real names).
    [property: JsonPropertyName("equipmentName")] string? EquipmentName,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("severity")] string Severity,
    [property: JsonPropertyName("detectedAt")] DateTime? DetectedAt,
    [property: JsonPropertyName("anomalySummary")] JsonElement? AnomalySummary,
    [property: JsonPropertyName("resolvedAt")] DateTime? ResolvedAt,
    // Prompt 9: cumulative estimated LLM cost for this incident (USD).
    [property: JsonPropertyName("estimatedCostUsd")] decimal? EstimatedCostUsd);

public sealed record IncidentListResult(
    [property: JsonPropertyName("items")] IReadOnlyList<IncidentDto> Items,
    [property: JsonPropertyName("total")] int Total,
    [property: JsonPropertyName("limit")] int Limit,
    [property: JsonPropertyName("offset")] int Offset);

public sealed class AgentServiceClient : IAgentServiceClient
{
    private readonly HttpClient _http;
    private readonly ICorrelationContext _correlation;
    private readonly string _headerName;
    private readonly ILogger<AgentServiceClient> _logger;
    private readonly IHttpContextAccessor _httpContextAccessor;

    public AgentServiceClient(
        HttpClient http,
        ICorrelationContext correlation,
        MiddlewareConfig config,
        IHttpContextAccessor httpContextAccessor,
        ILogger<AgentServiceClient> logger)
    {
        _http = http;
        _correlation = correlation;
        _headerName = config.CorrelationHeader;
        _logger = logger;
        _httpContextAccessor = httpContextAccessor;
    }

    public async Task<AgentHealthStatus> GetHealthAsync(CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Get, "api/v1/health/live", authenticated: false);
        var response = await _http.SendAsync(request, ct);
        response.EnsureSuccessStatusCode();
        var payload = await response.Content.ReadFromJsonAsync<AgentHealthStatus>(cancellationToken: ct)
                      ?? new AgentHealthStatus("unknown");
        _logger.LogInformation("Agent service responded {Status} (correlation id {CorrelationId})",
            payload.Status, _correlation.CorrelationId);
        return payload;
    }

    // Prompt 7: the agent service independently re-validates the forwarded
    // token against the identity provider's public keys (defense in depth --
    // "internal" never means "trusted blindly"). 200 = valid, 401 otherwise.
    public async Task ValidateTokenAsync(string accessToken, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Post, "api/v1/auth/validate", authenticated: false);
        request.Content = JsonContent.Create(new { token = accessToken });
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.Unauthorized)
        {
            throw new AgentServiceException(
                response.StatusCode,
                await response.Content.ReadAsStringAsync(ct));
        }
        response.EnsureSuccessStatusCode();
    }

    public async Task<IncidentListResult> ListIncidentsAsync(
        string? status = null, string? siteId = null, int limit = 50, int offset = 0, CancellationToken ct = default)
    {
        var query = new List<string> { $"limit={limit}", $"offset={offset}" };
        if (!string.IsNullOrWhiteSpace(status)) query.Add($"status={Uri.EscapeDataString(status)}");
        if (!string.IsNullOrWhiteSpace(siteId)) query.Add($"site_id={Uri.EscapeDataString(siteId)}");

        using var request = BuildRequest(HttpMethod.Get, $"api/v1/incidents?{string.Join("&", query)}");
        var response = await _http.SendAsync(request, ct);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadFromJsonAsync<IncidentListResult>(cancellationToken: ct)
               ?? new IncidentListResult(Array.Empty<IncidentDto>(), 0, limit, offset);
    }

    public async Task<JsonElement?> GetIncidentAsync(string incidentId, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Get, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}");
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    // Prompt 3: the Knowledge Agent's retrieved evidence (SOP chunks +
    // matched historical incidents) for one incident.
    public async Task<JsonElement?> GetIncidentEvidenceAsync(string incidentId, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Get, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}/evidence");
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    // Prompt 3: the Root-Cause Agent's citation-checked conclusion.
    public async Task<JsonElement?> GetIncidentRootCauseAsync(string incidentId, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Get, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}/root-cause");
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    // Prompt 4: the Risk Agent's severity/consequences/HITL level.
    public async Task<JsonElement?> GetIncidentRiskAsync(string incidentId, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Get, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}/risk");
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    // Prompt 4: the Recommendation Agent's actions + visible tool-call log.
    public async Task<JsonElement?> GetIncidentRecommendationAsync(string incidentId, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Get, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}/recommendation");
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    // Prompt 4: the incident's full state-machine transition history.
    public async Task<JsonElement?> GetIncidentStateHistoryAsync(string incidentId, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Get, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}/state-history");
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    // Prompt 4: approve/reject. `action` is "approve" or "reject". The body
    // carries decided_by + justification; identity comes from the JWT at the
    // controller layer so it can never be spoofed by the caller.
    public async Task<JsonElement?> PostIncidentDecisionAsync(
        string incidentId, string action, object body, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Post, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}/{action}");
        request.Content = JsonContent.Create(body);
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        if (!response.IsSuccessStatusCode)
        {
            // Keep the real error payload -- the HITL endpoints return
            // meaningful 409/422 details the caller should see.
            throw new AgentServiceException(
                response.StatusCode,
                await response.Content.ReadAsStringAsync(ct));
        }
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    // Prompt 5: the Action Agent's execution record (what was executed, CMMS
    // references, per-operation outcomes). Read passthrough only -- the
    // middleware NEVER talks to the mock CMMS itself (docs/security-model.md).
    public async Task<JsonElement?> GetIncidentActionAsync(string incidentId, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Get, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}/action");
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    // Prompt 5: manual resolution (no CMMS work order, or operator override).
    public async Task<JsonElement?> ResolveIncidentAsync(string incidentId, object body, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Post, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}/resolve");
        request.Content = JsonContent.Create(body);
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        if (!response.IsSuccessStatusCode)
        {
            // Carry the 409/422 detail through (same pattern as the decision
            // endpoints) so the caller sees why a resolve was refused.
            throw new AgentServiceException(
                response.StatusCode,
                await response.Content.ReadAsStringAsync(ct));
        }
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    // Prompt 6: grounded operator Q&A. Single LLM round trip on the agent
    // service, restricted to the incident's recorded evidence; 504 if the
    // model is slow. Error bodies are carried through like the decision
    // endpoints so the UI can show the real reason.
    public async Task<JsonElement?> AskAboutIncidentAsync(string incidentId, object body, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Post, $"api/v1/incidents/{Uri.EscapeDataString(incidentId)}/ask");
        request.Content = JsonContent.Create(body);
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        if (!response.IsSuccessStatusCode)
        {
            throw new AgentServiceException(
                response.StatusCode,
                await response.Content.ReadAsStringAsync(ct));
        }
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    public async Task<JsonElement?> GetLatestSensorReadingsAsync(string siteId, CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Get, $"api/v1/sites/{Uri.EscapeDataString(siteId)}/sensors/latest");
        var response = await _http.SendAsync(request, ct);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound) return null;
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    public async Task<JsonElement?> TriggerCoolingTowerScenarioAsync(CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Post, "api/v1/simulator/trigger-scenario/cooling-tower-incident");
        var response = await _http.SendAsync(request, ct);
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    public async Task<JsonElement?> ResetSimulatorAsync(CancellationToken ct = default)
    {
        using var request = BuildRequest(HttpMethod.Post, "api/v1/simulator/reset");
        var response = await _http.SendAsync(request, ct);
        response.EnsureSuccessStatusCode();
        using var doc = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        return doc.RootElement.Clone();
    }

    /// <summary>
    /// Builds a request with the correlation ID of the current HttpContext
    /// (if any) and -- for authenticated calls -- the CALLER'S OWN bearer
    /// token (Prompt 7): the agent service re-validates the token itself and
    /// enforces site scope + approval policy as a second layer, so the
    /// user's identity travels end to end (docs/auth-flow.md).
    /// </summary>
    private HttpRequestMessage BuildRequest(HttpMethod method, string path, bool authenticated = true)
    {
        var request = new HttpRequestMessage(method, path);
        if (authenticated)
        {
            var token = _httpContextAccessor.HttpContext?.Request.Headers.Authorization.ToString();
            if (!string.IsNullOrEmpty(token))
            {
                request.Headers.Authorization = System.Net.Http.Headers.AuthenticationHeaderValue.Parse(token);
            }
        }
        if (!string.IsNullOrEmpty(_correlation.CorrelationId) && _correlation.CorrelationId != "no-correlation-id")
        {
            request.Headers.Add(_headerName, _correlation.CorrelationId);
        }
        return request;
    }
}
