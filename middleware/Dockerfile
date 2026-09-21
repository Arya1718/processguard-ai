# ---------------------------------------------------------------------------
# ProcessGuard AI — .NET 8 middleware image
# Multi-stage: SDK build stage, then ASP.NET runtime (non-root) runtime stage.
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src

# Restore layer first for cache efficiency
COPY src/ProcessGuard.Middleware.csproj .
RUN dotnet restore ProcessGuard.Middleware.csproj

COPY src/ .
RUN dotnet publish ProcessGuard.Middleware.csproj -c Release -o /app/publish /p:UseAppHost=false

# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS final
WORKDIR /app

# curl for the container healthcheck (startup gate for the Python services)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (the aspnet image includes uid 1654 app user)
USER app

COPY --from=build /app/publish .

ENV ASPNETCORE_URLS=http://+:8080
EXPOSE 8080

ENTRYPOINT ["dotnet", "ProcessGuard.Middleware.dll"]
