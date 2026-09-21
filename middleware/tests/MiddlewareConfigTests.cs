using ProcessGuard.Middleware.Infrastructure;
using Xunit;

namespace ProcessGuard.Middleware.Tests;

public class MiddlewareConfigTests
{
    [Theory]
    [InlineData("PGAI_OIDC__AUTHORITY", "https://idp.example.com")]
    [InlineData("PGAI_OIDC__AUDIENCE", "test-audience")]
    [InlineData("PGAI_OIDC__ISSUER", "https://idp.example.com")]
    [InlineData("PGAI_CONNECTIONSTRINGS__POSTGRES", "postgres://test")]
    [InlineData("PGAI_CONNECTIONSTRINGS__REDIS", "redis://test")]
    [InlineData("PGAI_AGENTSERVICE__BASEURL", "http://agent:8000")]
    public void Load_Throws_When_RequiredVarMissing(string missingKey, string placeholder)
    {
        // Save and clear env
        var saved = Environment.GetEnvironmentVariable(missingKey);
        Environment.SetEnvironmentVariable(missingKey, null);

        // Ensure all other required vars are set
        Environment.SetEnvironmentVariable("PGAI_OIDC__AUTHORITY", "https://idp.example.com");
        Environment.SetEnvironmentVariable("PGAI_OIDC__AUDIENCE", "test-audience");
        Environment.SetEnvironmentVariable("PGAI_OIDC__ISSUER", "https://idp.example.com");
        Environment.SetEnvironmentVariable("PGAI_OIDC__INTERNALISSUER", "https://internal-idp");
        Environment.SetEnvironmentVariable("PGAI_CONNECTIONSTRINGS__POSTGRES", "postgres://test");
        Environment.SetEnvironmentVariable("PGAI_CONNECTIONSTRINGS__REDIS", "redis://test");
        Environment.SetEnvironmentVariable("PGAI_AGENTSERVICE__BASEURL", "http://agent:8000");
        Environment.SetEnvironmentVariable("PGAI_RBAC__POLICYFILE", placeholder);

        // Clear the key under test
        Environment.SetEnvironmentVariable(missingKey, null);

        Assert.Throws<ClearConfigException>(() => MiddlewareConfig.Load());

        // Restore
        if (saved != null)
            Environment.SetEnvironmentVariable(missingKey, saved);
    }

    [Fact]
    public void Load_SucceedsWithAllVars()
    {
        Environment.SetEnvironmentVariable("PGAI_OIDC__AUTHORITY", "https://idp.example.com");
        Environment.SetEnvironmentVariable("PGAI_OIDC__AUDIENCE", "test-audience");
        Environment.SetEnvironmentVariable("PGAI_OIDC__ISSUER", "https://idp.example.com");
        Environment.SetEnvironmentVariable("PGAI_OIDC__INTERNALISSUER", "https://internal-idp");
        Environment.SetEnvironmentVariable("PGAI_OIDC__REQUIREHTTPSMETADATA", "true");
        Environment.SetEnvironmentVariable("PGAI_CONNECTIONSTRINGS__POSTGRES", "postgres://test");
        Environment.SetEnvironmentVariable("PGAI_CONNECTIONSTRINGS__REDIS", "redis://test");
        Environment.SetEnvironmentVariable("PGAI_AGENTSERVICE__BASEURL", "http://agent:8000");
        Environment.SetEnvironmentVariable("PGAI_RBAC__POLICYFILE", "config/rbac-policy.json");
        Environment.SetEnvironmentVariable("PGAI_CORRELATION_HEADER", "X-Correlation-Id");

        var config = MiddlewareConfig.Load();

        Assert.Equal("https://idp.example.com", config.Oidc.Authority);
        Assert.Equal("test-audience", config.Oidc.Audience);
        Assert.True(config.Oidc.RequireHttpsMetadata);
        Assert.Equal("X-Correlation-Id", config.CorrelationHeader);
    }

    [Fact]
    public void OidcOptions_ValidIssuers_ReturnsBoth()
    {
        var options = new OidcOptions
        {
            Issuer = "https://iss",
            InternalIssuer = "https://internal-iss",
        };
        var issuers = options.ValidIssuers;
        Assert.Contains("https://iss", issuers);
        Assert.Contains("https://internal-iss", issuers);
    }

    [Fact]
    public void OidcOptions_ValidIssuers_ReturnsSingle_WhenInternalNull()
    {
        var options = new OidcOptions
        {
            Issuer = "https://iss",
            InternalIssuer = string.Empty,
        };
        var issuers = options.ValidIssuers;
        Assert.Single(issuers);
        Assert.Contains("https://iss", issuers);
    }

    [Fact]
    public void ClearConfigException_InheritsFromException()
    {
        var ex = new ClearConfigException("test");
        Assert.IsType<Exception>(ex);
        Assert.Equal("test", ex.Message);
    }
}
