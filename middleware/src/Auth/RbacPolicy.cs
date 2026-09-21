using System.Text.Json;
using Microsoft.AspNetCore.Authorization;
using System.Security.Claims;
using ProcessGuard.Middleware.Infrastructure;

namespace ProcessGuard.Middleware.Auth;

/// <summary>
/// The shared role-to-capability policy table (config/rbac-policy.json).
/// The SAME file is loaded by the Python agent-service, which re-checks
/// decisions as a second layer -- the two services are configured from one
/// source of truth (docs/rbac-policy.md). Loaded from EXTERNAL CONFIG so
/// roles can be tuned without a code change (same discipline as the HITL
/// threshold table from Prompt 4).
/// </summary>
public sealed class RbacPolicy
{
    public Dictionary<string, RolePolicy> Roles { get; init; } = new();
    public List<string> IncidentReadRoles { get; init; } = new();
    public List<string> ChatRoles { get; init; } = new();
    public List<string> ResolveRoles { get; init; } = new();
    public List<string> SimulatorRoles { get; init; } = new();

    public sealed class RolePolicy
    {
        public int MaxApprovalLevel { get; init; }
        public string Description { get; init; } = string.Empty;
    }

    public static RbacPolicy Load(string path)
    {
        if (!File.Exists(path))
        {
            throw new ClearConfigException(
                $"RBAC policy file not found at '{path}'. " +
                "Set PGAI_RBAC__POLICYFILE to the config/rbac-policy.json path " +
                "(mounted into the container by docker-compose.yml).");
        }
        var json = File.ReadAllText(path);
        using var doc = JsonDocument.Parse(json, new JsonDocumentOptions
        {
            AllowTrailingCommas = true,
            CommentHandling = JsonCommentHandling.Skip,
        });
        var root = doc.RootElement;

        var roles = new Dictionary<string, RolePolicy>();
        foreach (var prop in root.GetProperty("roles").EnumerateObject())
        {
            roles[prop.Name] = new RolePolicy
            {
                MaxApprovalLevel = prop.Value.GetProperty("max_approval_level").GetInt32(),
                Description = prop.Value.TryGetProperty("description", out var d) ? d.GetString() ?? "" : "",
            };
        }

        List<string> StrArray(string prop) => root.TryGetProperty(prop, out var arr)
            ? arr.EnumerateArray().Select(x => x.GetString() ?? "").Where(x => x.Length > 0).ToList()
            : new List<string>();

        return new RbacPolicy
        {
            Roles = roles,
            IncidentReadRoles = StrArray("incident_read_roles"),
            ChatRoles = StrArray("chat_roles"),
            ResolveRoles = StrArray("resolve_roles"),
            SimulatorRoles = StrArray("simulator_roles"),
        };
    }

    /// <summary>Highest HITL level this role may decide (0 = none).</summary>
    public int MaxApprovalLevel(string? role) =>
        role is not null && Roles.TryGetValue(role, out var policy) ? policy.MaxApprovalLevel : 0;

    public bool CanReadIncidents(string? role) => role is not null && IncidentReadRoles.Contains(role);
    public bool CanChat(string? role) => role is not null && ChatRoles.Contains(role);
    public bool CanResolve(string? role) => role is not null && ResolveRoles.Contains(role);
    public bool CanControlSimulator(string? role) => role is not null && SimulatorRoles.Contains(role);
}

/// <summary>
/// Prompt 7 RBAC: maps the token's role claim onto ASP.NET Core authorization
/// policies built from the external policy table. Site scoping is enforced in
/// the controllers/site-scope service (the policy layer covers capabilities).
/// </summary>
public static class RbacPolicies
{
    public const string IncidentRead = "IncidentRead";
    public const string Chat = "Chat";
    public const string Simulator = "SimulatorControl";
    public const string Resolve = "IncidentResolve";
    public const string ApproveLevel2 = "ApproveLevel2";
    public const string ApproveLevel3 = "ApproveLevel3";

    public static void Register(AuthorizationOptions options, RbacPolicy policy)
    {
        options.AddPolicy(IncidentRead, b => b.RequireAssertion(
            ctx => policy.CanReadIncidents(Role(ctx.User))));
        options.AddPolicy(Chat, b => b.RequireAssertion(
            ctx => policy.CanChat(Role(ctx.User))));
        options.AddPolicy(Simulator, b => b.RequireAssertion(
            ctx => policy.CanControlSimulator(Role(ctx.User))));
        options.AddPolicy(Resolve, b => b.RequireAssertion(
            ctx => policy.CanResolve(Role(ctx.User))));
        options.AddPolicy(ApproveLevel2, b => b.RequireAssertion(
            ctx => policy.MaxApprovalLevel(Role(ctx.User)) >= 2));
        options.AddPolicy(ApproveLevel3, b => b.RequireAssertion(
            ctx => policy.MaxApprovalLevel(Role(ctx.User)) >= 3));
    }

    /// <summary>The token's role claim (Entra-style `roles` array claim;
    /// falls back to classic role claims for other token setups).</summary>
    public static string? Role(ClaimsPrincipal user)
    {
        var rolesClaim = user.FindFirst("roles") ?? user.FindFirst(ClaimTypes.Role);
        if (rolesClaim is null) return null;
        var value = rolesClaim.Value;
        // JsonElement-style arrays ("[\"Role\"]") and comma lists both handled.
        if (value.StartsWith('['))
        {
            try
            {
                using var doc = JsonDocument.Parse(value);
                return doc.RootElement.EnumerateArray().Select(e => e.GetString()).FirstOrDefault(v => !string.IsNullOrEmpty(v));
            }
            catch (JsonException)
            {
                return null;
            }
        }
        return value.Split(',')[0].Trim();
    }
}

/// <summary>Accessors for the Prompt 7 claims on the validated principal.</summary>
public static class PrincipalExtensions
{
    public static string? SiteId(this ClaimsPrincipal user) =>
        user.FindFirst("site_id")?.Value;

    public static string? DisplayName(this ClaimsPrincipal user) =>
        user.FindFirst("name")?.Value ?? user.FindFirst("preferred_username")?.Value ?? user.Identity?.Name;
}
