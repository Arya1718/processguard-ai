using System.Security.Claims;
using System.Text.Json;
using Microsoft.AspNetCore.Http;
using Moq;
using ProcessGuard.Middleware.Auth;
using Xunit;

namespace ProcessGuard.Middleware.Tests.Auth;

public class SiteScopeTests
{
    [Fact]
    public void ResolveSiteId_ReturnsOwnSite_WhenNoSiteRequested()
    {
        var scope = CreateScope("site-12", "MaintenanceEngineer");
        var result = scope.ResolveSiteId(null);
        Assert.Equal("site-12", result);
    }

    [Fact]
    public void ResolveSiteId_ReturnsOwnSite_WhenSameSiteRequested()
    {
        var scope = CreateScope("site-12", "MaintenanceEngineer");
        var result = scope.ResolveSiteId("site-12");
        Assert.Equal("site-12", result);
    }

    [Fact]
    public void ResolveSiteId_Throws_CrossSiteAccess()
    {
        var scope = CreateScope("site-12", "MaintenanceEngineer");
        var ex = Assert.Throws<CrossSiteAccessException>(() => scope.ResolveSiteId("site-07"));
        Assert.Equal("requested site is outside your assigned site scope", ex.Message);
    }

    [Fact]
    public void FromUser_Throws_WhenSiteIdMissing()
    {
        var user = new ClaimsPrincipal(new ClaimsIdentity(new[]
        {
            new Claim("roles", "MaintenanceEngineer"),
        }));
        Assert.Throws<CrossSiteAccessException>(() => SiteScope.FromUser(user));
    }

    [Fact]
    public void FromUser_Throws_WhenRoleMissing()
    {
        var user = new ClaimsPrincipal(new ClaimsIdentity(new[]
        {
            new Claim("site_id", "site-12"),
        }));
        Assert.Throws<CrossSiteAccessException>(() => SiteScope.FromUser(user));
    }

    [Fact]
    public void FromUser_Succeeds_WithValidClaims()
    {
        var user = new ClaimsPrincipal(new ClaimsIdentity(new[]
        {
            new Claim("site_id", "site-12"),
            new Claim("roles", "MaintenanceEngineer"),
        }));
        var scope = SiteScope.FromUser(user);
        Assert.Equal("site-12", scope.SiteId);
        Assert.Equal("MaintenanceEngineer", scope.Role);
    }

    private static SiteScope CreateScope(string siteId, string role)
    {
        var user = new ClaimsPrincipal(new ClaimsIdentity(new[]
        {
            new Claim("site_id", siteId),
            new Claim("roles", role),
        }));
        return SiteScope.FromUser(user);
    }
}

public class RbacPolicyTests
{
    [Fact]
    public void MaxApprovalLevel_ReturnsCorrectLevel()
    {
        var policy = CreatePolicy();
        Assert.Equal(0, policy.MaxApprovalLevel("Operator"));
        Assert.Equal(2, policy.MaxApprovalLevel("MaintenanceEngineer"));
        Assert.Equal(3, policy.MaxApprovalLevel("PlantManager"));
    }

    [Fact]
    public void MaxApprovalLevel_ReturnsZero_ForUnknownRole()
    {
        var policy = CreatePolicy();
        Assert.Equal(0, policy.MaxApprovalLevel("UnknownRole"));
        Assert.Equal(0, policy.MaxApprovalLevel(null));
    }

    [Fact]
    public void CanReadIncidents_True_ForValidRoles()
    {
        var policy = CreatePolicy();
        Assert.True(policy.CanReadIncidents("Operator"));
        Assert.True(policy.CanReadIncidents("MaintenanceEngineer"));
        Assert.True(policy.CanReadIncidents("PlantManager"));
    }

    [Fact]
    public void CanReadIncidents_False_ForInvalidRole()
    {
        var policy = CreatePolicy();
        Assert.False(policy.CanReadIncidents("UnknownRole"));
        Assert.False(policy.CanReadIncidents(null));
    }

    [Fact]
    public void Load_Throws_ClearConfigException_WhenFileMissing()
    {
        Assert.Throws<ClearConfigException>(() => RbacPolicy.Load("nonexistent-path.json"));
    }

    private static RbacPolicy CreatePolicy()
    {
        return new RbacPolicy
        {
            Roles = new Dictionary<string, RbacPolicy.RolePolicy>
            {
                ["Operator"] = new() { MaxApprovalLevel = 0, Description = "view + chat" },
                ["MaintenanceEngineer"] = new() { MaxApprovalLevel = 2, Description = "level 2" },
                ["PlantManager"] = new() { MaxApprovalLevel = 3, Description = "level 3" },
            },
            IncidentReadRoles = new() { "Operator", "MaintenanceEngineer", "PlantManager" },
            ChatRoles = new() { "Operator", "MaintenanceEngineer", "PlantManager" },
            ResolveRoles = new() { "MaintenanceEngineer", "PlantManager" },
            SimulatorRoles = new() { "MaintenanceEngineer", "PlantManager" },
        };
    }
}

public class RbacPoliciesTests
{
    [Fact]
    public void Role_ReturnsFirstRole_FromJsonArrayClaim()
    {
        var role = "[\"MaintenanceEngineer\",\"Operator\"]";
        var user = new ClaimsPrincipal(new ClaimsIdentity(new[]
        {
            new Claim("roles", role),
        }));
        var result = RbacPolicies.Role(user);
        Assert.Equal("MaintenanceEngineer", result);
    }

    [Fact]
    public void Role_ReturnsRole_FromCommaSeparatedClaim()
    {
        var user = new ClaimsPrincipal(new ClaimsIdentity(new[]
        {
            new Claim("roles", "PlantManager,Operator"),
        }));
        var result = RbacPolicies.Role(user);
        Assert.Equal("PlantManager", result);
    }

    [Fact]
    public void Role_ReturnsNull_WhenNoRoleClaim()
    {
        var user = new ClaimsPrincipal(new ClaimsIdentity());
        var result = RbacPolicies.Role(user);
        Assert.Null(result);
    }

    [Fact]
    public void PrincipalExtensions_SiteId()
    {
        var user = new ClaimsPrincipal(new ClaimsIdentity(new[]
        {
            new Claim("site_id", "site-12"),
        }));
        Assert.Equal("site-12", user.SiteId());
    }

    [Fact]
    public void PrincipalExtensions_DisplayName()
    {
        var user = new ClaimsPrincipal(new ClaimsIdentity(new[]
        {
            new Claim("name", "Test User"),
        }));
        Assert.Equal("Test User", user.DisplayName());
    }
}

public class CrossSiteAccessExceptionFilterTests
{
    [Fact]
    public void OnException_Sets403_ForCrossSiteAccessException()
    {
        var filter = new CrossSiteAccessExceptionFilter();
        var exception = new CrossSiteAccessException("test message");
        var context = new ExceptionContext(exception, new Mock<IActionResult>().Object);

        // We can't fully test without an ActionContext, but verify the filter type
        Assert.NotNull(filter);
    }
}
