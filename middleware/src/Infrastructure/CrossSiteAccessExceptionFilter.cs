using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Filters;
using ProcessGuard.Middleware.Auth;

namespace ProcessGuard.Middleware.Infrastructure;

/// <summary>
/// Renders a cross-site access attempt as 403 with a clear message -- the
/// prompt requires a policy refusal, not a misleading 404.
/// </summary>
public sealed class CrossSiteAccessExceptionFilter : IExceptionFilter
{
    public void OnException(ExceptionContext context)
    {
        if (context.Exception is CrossSiteAccessException crossSite)
        {
            context.Result = new ObjectResult(new { error = crossSite.Message })
            {
                StatusCode = StatusCodes.Status403Forbidden,
            };
            context.ExceptionHandled = true;
        }
    }
}
