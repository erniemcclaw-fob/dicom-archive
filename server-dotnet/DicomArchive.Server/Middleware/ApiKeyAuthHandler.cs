using System.Security.Claims;
using System.Text.Encodings.Web;
using Microsoft.AspNetCore.Authentication;
using Microsoft.Extensions.Options;

namespace DicomArchive.Server.Middleware;

/// <summary>
/// Validates the X-Api-Key header against the AGENT_API_KEY environment variable.
/// Applied to the "AgentApiKey" authentication scheme used by ingest endpoints.
/// </summary>
public class ApiKeyAuthHandler(
    IOptionsMonitor<AuthenticationSchemeOptions> options,
    ILoggerFactory loggerFactory,
    UrlEncoder encoder,
    IConfiguration config)
    : AuthenticationHandler<AuthenticationSchemeOptions>(options, loggerFactory, encoder)
{
    public const string SchemeName = "AgentApiKey";
    public const string HeaderName = "X-Api-Key";

    protected override Task<AuthenticateResult> HandleAuthenticateAsync()
    {
        var expectedKey = config["AGENT_API_KEY"];
        if (string.IsNullOrEmpty(expectedKey))
            return Task.FromResult(AuthenticateResult.Fail("AGENT_API_KEY is not configured on the server"));

        if (!Request.Headers.TryGetValue(HeaderName, out var providedKey) || string.IsNullOrEmpty(providedKey))
            return Task.FromResult(AuthenticateResult.Fail("Missing X-Api-Key header"));

        if (!string.Equals(expectedKey, providedKey, StringComparison.Ordinal))
            return Task.FromResult(AuthenticateResult.Fail("Invalid API key"));

        var identity = new ClaimsIdentity(SchemeName);
        identity.AddClaim(new Claim(ClaimTypes.Name, "agent"));
        var principal = new ClaimsPrincipal(identity);
        var ticket = new AuthenticationTicket(principal, SchemeName);

        return Task.FromResult(AuthenticateResult.Success(ticket));
    }
}
