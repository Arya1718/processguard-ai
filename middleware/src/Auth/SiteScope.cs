using ProcessGuard.Middleware.Clients;

namespace ProcessGuard.Middleware.Auth;

/// <summary>
/// Thrown when a caller asks for another site's data. Rendered as 403 by
/// the exception handler -- a cross-site access attempt is a POLICY refusal,
/// not a lookup miss (docs/security-model.md).
/// </summary>
public sealed class CrossSiteAccessException : Exception
{
    public CrossSiteAccessException(string message) : base(message) { }
}

/// <summary>
/// The per-request site scope derived from the VALIDATED token. Every
/// incident-affecting call is forced through here, so the site_id predicate
/// is applied at the QUERY layer (the .NET middleware overwrites/clamps the
/// requested site), not just at the route layer -- a caller cannot bypass it
/// by passing ?siteId=... for another site or by addressing an incident id
/// that belongs to another site (docs/security-model.md).
/// </summary>
public sealed class SiteScope
{
    public string SiteId { get; }
    public string Role { get; }

    private SiteScope(string siteId, string role)
    {
        SiteId = siteId;
        Role = role;
    }

    /// <summary>Builds the scope from the validated principal; throws if the
    /// token lacks the site/role claims (i.e. was not issued for this API).</summary>
    public static SiteScope FromUser(System.Security.Claims.ClaimsPrincipal user)
    {
        var siteId = user.SiteId();
        var role = RbacPolicies.Role(user);
        if (string.IsNullOrWhiteSpace(siteId) || string.IsNullOrWhiteSpace(role))
        {
            throw new CrossSiteAccessException(
                "token carries no site_id/role claims; cannot establish a data scope");
        }
        return new SiteScope(siteId, role);
    }

    /// <summary>
    /// The ONLY site this request may touch. Rejects an explicit request for
    /// another site (fail closed rather than silently filter).
    /// </summary>
    public string ResolveSiteId(string? requestedSiteId)
    {
        if (string.IsNullOrWhiteSpace(requestedSiteId)) return SiteId;
        if (!string.Equals(requestedSiteId, SiteId, StringComparison.OrdinalIgnoreCase))
        {
            throw new CrossSiteAccessException(
                "requested site is outside your assigned site scope");
        }
        return SiteId;
    }
}
