using System.Text.Json;
using DicomArchive.Server.Data;
using DicomArchive.Server.Endpoints;
using DicomArchive.Server.Middleware;
using DicomArchive.Server.Services;
using Microsoft.AspNetCore.Authentication;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);

// ── Aspire service defaults ───────────────────────────────────────────────────
// Adds OpenTelemetry tracing + metrics, health checks, service discovery,
// and structured logging. This is the line that lights up the Aspire dashboard.
builder.AddServiceDefaults();

// ── Database ──────────────────────────────────────────────────────────────────
// When running under Aspire, the connection string is injected automatically
// from the AppHost's postgres resource reference.
// When running standalone, it reads from CONNECTIONSTRINGS__dicom-archive
// or DATABASE_URL in the environment.
//
// We register both:
//  - AddNpgsqlDbContext  → scoped ArchiveDbContext for Minimal API endpoints
//  - AddDbContextFactory → IDbContextFactory for RouterService + QueueProcessorService
//    (background services need to create their own scopes)
builder.AddNpgsqlDbContext<ArchiveDbContext>("dicom-archive");
builder.Services.AddDbContextFactory<ArchiveDbContext>(lifetime: ServiceLifetime.Scoped);

// ── Authentication ────────────────────────────────────────────────────────────
builder.Services
    .AddAuthentication(ApiKeyAuthHandler.SchemeName)
    .AddScheme<AuthenticationSchemeOptions, ApiKeyAuthHandler>(
        ApiKeyAuthHandler.SchemeName, _ => { });

builder.Services.AddAuthorizationBuilder()
    .AddPolicy("AgentPolicy", policy =>
        policy.RequireAuthenticatedUser()
              .AddAuthenticationSchemes(ApiKeyAuthHandler.SchemeName));

// ── Application services ──────────────────────────────────────────────────────
builder.Services.AddScoped<RouterService>();
builder.Services.AddScoped<StorageService>();
builder.Services.AddHostedService<QueueProcessorService>();
builder.Services.AddCors(o => o.AddDefaultPolicy(p => p.AllowAnyOrigin().AllowAnyMethod().AllowAnyHeader()));
builder.Services.ConfigureHttpJsonOptions(o =>
    o.SerializerOptions.PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower);

// Register the DicomArchive.Router ActivitySource for OpenTelemetry tracing
builder.Services.AddOpenTelemetry()
    .WithTracing(t => t.AddSource("DicomArchive.Router"));

var app = builder.Build();

// ── Middleware ────────────────────────────────────────────────────────────────
app.UseCors();
app.UseDefaultFiles();
app.UseStaticFiles();   // serves wwwroot/index.html
app.MapDefaultEndpoints(); // Aspire health + liveness probes
app.UseAuthentication();
app.UseAuthorization();

// ── Schema initialization ─────────────────────────────────────────────────────
// Run DDL on startup so the .NET server can work with a fresh Aspire Postgres
// container. All statements are CREATE TABLE IF NOT EXISTS — safe on existing DBs.
using (var scope = app.Services.CreateScope())
{
    var initLogger = scope.ServiceProvider.GetRequiredService<ILogger<Program>>();
    await SchemaInitializer.RunAsync(scope.ServiceProvider, initLogger);
}

// ── API routes ────────────────────────────────────────────────────────────────
StudyEndpoints.Map(app);
DestinationEndpoints.Map(app);
RuleEndpoints.Map(app);
AgentEndpoints.Map(app);
IngestEndpoints.Map(app);
WadoEndpoints.Map(app);

app.Run();
