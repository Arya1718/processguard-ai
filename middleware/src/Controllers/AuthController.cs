using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using ProcessGuard.Middleware.Auth;
using ProcessGuard.Middleware.Clients;
using ProcessGuard.Middleware.Infrastructure;

namespace ProcessGuard.Middleware.Controllers;

/// <summary>
/// OIDC sign-in relay (Prompt 7). The frontend runs authorization-code + PKCE
/// directly against the identity provider, then exchanges the code AT the
/// provider. The middleware never sees the code and holds no secrets for the
/// flow -- it only validates the resulting access token as a bearer JWT on
/// every API call.
///
/// This endpoint exists because the browser talks to the provider through
/// nginx only when the provider is host-published; to keep the demo
/// single-origin (no CORS), nginx also routes /oidc/* straight to the
/// provider. This relay is therefore a CONVENIENCE used by the e2e tests and
/// any deployment where the provider is not browser-reachable: it swaps a
/// valid provider access token for a middleware-session response carrying
/// the same validated claims (see ValidateAndProjectAsync below).
///
/// The dev-token endpoint from Prompt 1 is GONE -- real OIDC login is the
/// only way in.
/// </summary>
[ApiController]
[Route("api/v1/auth")]
public sealed class AuthController : ControllerBase
{
    public sealed record RelayRequest(string? AccessToken);

    private readonly IAgentServiceClient _agent;

    public AuthController(IAgentServiceClient agent) => _agent = agent;

    /// <summary>
    /// POST /api/v1/auth/session: { accessToken } obtained from the provider.
    /// The token is validated end to end (signature via JWKS, issuer,
    /// audience, lifetime) through the SAME typed client the middleware uses
    /// for every agent call -- the agent service re-validates it against the
    /// provider's public keys, so a token the provider never signed (or an
    /// expired one) is rejected here, not merely at the next API call.
    /// </summary>
    [HttpPost("session")]
    [AllowAnonymous]
    public async Task<IActionResult> Session([FromBody] RelayRequest request, CancellationToken ct)
    {
        if (request is null || string.IsNullOrWhiteSpace(request.AccessToken))
        {
            return BadRequest(new { error = "accessToken is required" });
        }

        // Round-trip validation through the agent service's OIDC validator
        // (which fetches the provider's JWKS). Returns 401 with the real
        // reason on any invalid token -- no local trust decision is made.
        try
        {
            await _agent.ValidateTokenAsync(request.AccessToken, ct);
        }
        catch (AgentServiceException ex) when (ex.StatusCode == System.Net.HttpStatusCode.Unauthorized)
        {
            return Unauthorized(new { error = "token rejected by identity validation", detail = ex.Body });
        }

        return Ok(new
        {
            tokenType = "Bearer",
            message = "validated; use the provider access token as the bearer token on API calls",
        });
    }

    /// <summary>
    /// GET /api/v1/auth/me: who the validated token says you are -- role,
    /// site, and what your role may approve (drives the role-aware UI).
    /// </summary>
    [HttpGet("me")]
    [Authorize]
    public IActionResult Me()
    {
        var role = RbacPolicies.Role(User);
        var policy = HttpContext.RequestServices.GetRequiredService<RbacPolicy>();
        return Ok(new
        {
            userName = User.DisplayName(),
            preferredUserName = User.FindFirst("preferred_username")?.Value ?? User.Identity?.Name,
            role,
            siteId = User.SiteId(),
            siteName = User.FindFirst("site_name")?.Value,
            maxApprovalLevel = policy.MaxApprovalLevel(role),
            canApprove = policy.MaxApprovalLevel(role) > 0,
        });
    }
}
