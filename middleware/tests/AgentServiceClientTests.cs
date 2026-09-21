using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using Moq;
using Moq.Protected;
using ProcessGuard.Middleware.Clients;
using ProcessGuard.Middleware.Infrastructure;
using Xunit;

namespace ProcessGuard.Middleware.Tests;

public class AgentServiceClientTests
{
    private readonly Mock<HttpMessageHandler> _handlerMock;
    private readonly HttpClient _http;
    private readonly Mock<ICorrelationContext> _correlation;
    private readonly MiddlewareConfig _config;
    private readonly Mock<IHttpContextAccessor> _httpContext;
    private readonly AgentServiceClient _client;

    public AgentServiceClientTests()
    {
        _handlerMock = new Mock<HttpMessageHandler>();
        _http = new HttpClient(_handlerMock.Object) { BaseAddress = new Uri("http://test") };
        _correlation = new Mock<ICorrelationContext>();
        _correlation.Setup(c => c.CorrelationId).Returns("test-corr-id");
        _config = new MiddlewareConfig
        {
            CorrelationHeader = "X-Correlation-Id",
            AgentService = new AgentServiceOptions { BaseUrl = "http://test" },
        };
        _httpContext = new Mock<IHttpContextAccessor>();
        var logger = new Mock<ILogger<AgentServiceClient>>();
        _client = new AgentServiceClient(
            _http, _correlation.Object, _config, _httpContext.Object, logger.Object);
    }

    [Fact]
    public async Task GetHealthAsync_ReturnsStatus()
    {
        var response = new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = JsonContent.Create(new AgentHealthStatus("ok")),
        };
        _handlerMock.Protected()
            .Setup<Task<HttpResponseMessage>>(
                "SendAsync",
                ItExpr.IsAny<HttpRequestMessage>(),
                ItExpr.IsAny<CancellationToken>()
            )
            .ReturnsAsync(response);

        var result = await _client.GetHealthAsync();
        Assert.Equal("ok", result.Status);
    }

    [Fact]
    public async Task ValidateTokenAsync_Success()
    {
        var response = new HttpResponseMessage(HttpStatusCode.OK);
        _handlerMock.Protected()
            .Setup<Task<HttpResponseMessage>>(
                "SendAsync",
                ItExpr.IsAny<HttpRequestMessage>(),
                ItExpr.IsAny<CancellationToken>()
            )
            .ReturnsAsync(response);

        await _client.ValidateTokenAsync("test-token");
    }

    [Fact]
    public async Task ValidateTokenAsync_Throws_On401()
    {
        var response = new HttpResponseMessage(HttpStatusCode.Unauthorized)
        {
            Content = new StringContent("invalid"),
        };
        _handlerMock.Protected()
            .Setup<Task<HttpResponseMessage>>(
                "SendAsync",
                ItExpr.IsAny<HttpRequestMessage>(),
                ItExpr.IsAny<CancellationToken>()
            )
            .ReturnsAsync(response);

        await Assert.ThrowsAsync<AgentServiceException>(() => _client.ValidateTokenAsync("bad-token"));
    }

    [Fact]
    public async Task ListIncidentsAsync_ReturnsResult()
    {
        var response = new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = JsonContent.Create(new IncidentListResult(
                Array.Empty<IncidentDto>(), 0, 50, 0)),
        };
        _handlerMock.Protected()
            .Setup<Task<HttpResponseMessage>>(
                "SendAsync",
                ItExpr.IsAny<HttpRequestMessage>(),
                ItExpr.IsAny<CancellationToken>()
            )
            .ReturnsAsync(response);

        var result = await _client.ListIncidentsAsync("open", "site-12");
        Assert.Equal(0, result.Total);
    }

    [Fact]
    public async Task GetIncidentAsync_ReturnsJson_WhenFound()
    {
        var json = JsonSerializer.Serialize(new { id = "inc-1", status = "open" });
        var response = new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(json),
        };
        _handlerMock.Protected()
            .Setup<Task<HttpResponseMessage>>(
                "SendAsync",
                ItExpr.IsAny<HttpRequestMessage>(),
                ItExpr.IsAny<CancellationToken>()
            )
            .ReturnsAsync(response);

        var result = await _client.GetIncidentAsync("inc-1");
        Assert.NotNull(result);
    }

    [Fact]
    public async Task GetIncidentAsync_ReturnsNull_When404()
    {
        var response = new HttpResponseMessage(HttpStatusCode.NotFound);
        _handlerMock.Protected()
            .Setup<Task<HttpResponseMessage>>(
                "SendAsync",
                ItExpr.IsAny<HttpRequestMessage>(),
                ItExpr.IsAny<CancellationToken>()
            )
            .ReturnsAsync(response);

        var result = await _client.GetIncidentAsync("nonexistent");
        Assert.Null(result);
    }

    [Fact]
    public void AgentServiceException_StoresBody()
    {
        var ex = new AgentServiceException(HttpStatusCode.BadRequest, "error details");
        Assert.Equal("error details", ex.Body);
    }

    [Fact]
    public void IncidentDto_HasEstimatedCost()
    {
        var dto = new IncidentDto("id", "site", "eq", "Eq", "open", "high", null, null, 1.5m);
        Assert.Equal(1.5m, dto.EstimatedCostUsd);
    }
}
