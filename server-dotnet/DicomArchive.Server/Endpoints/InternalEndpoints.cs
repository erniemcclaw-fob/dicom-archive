using DicomArchive.Server.Data;
using DicomArchive.Server.Services;
using Microsoft.EntityFrameworkCore;

namespace DicomArchive.Server.Endpoints;

/// <summary>
/// Endpoints called by the Python ingest agent, not by the web UI.
/// </summary>
public static class InternalEndpoints
{
    public static void Map(WebApplication app)
    {
        app.MapPost("/internal/register",  Register);
        app.MapPost("/internal/heartbeat", Heartbeat);
        app.MapPost("/internal/routed",    OnInstanceReceived);

        app.MapPost("/api/route/instance/{instanceUid}/to/{destId:int}", RouteInstance);
        app.MapPost("/api/route/study/{studyUid}/to/{destId:int}",       RouteStudy);
    }

    static async Task<IResult> Register(ArchiveDbContext db, AgentRegistration? body,
        ILogger<Program> logger)
    {
        if (body?.AeTitle is null) return Results.BadRequest("Missing body or ae_title");
        var ae = body.AeTitle.ToUpper();

        var agent = await db.Agents.FirstOrDefaultAsync(a => a.AeTitle == ae);
        if (agent is null)
        {
            agent = new Agent { AeTitle = ae, FirstSeen = DateTime.UtcNow };
            db.Agents.Add(agent);
        }

        agent.Host           = body.Host;
        agent.StorageBackend = body.StorageBackend ?? agent.StorageBackend;
        agent.Version        = body.Version        ?? agent.Version;
        agent.LastSeen       = DateTime.UtcNow;

        await db.SaveChangesAsync();
        logger.LogInformation("Agent registered: [{AeTitle}] from {Host}", ae, body.Host);

        return Results.Ok(new { ok = true, agent });
    }

    static async Task<IResult> Heartbeat(ArchiveDbContext db, AgentHeartbeat? body)
    {
        if (body?.AeTitle is null) return Results.BadRequest("Missing body or ae_title");
        var ae = body.AeTitle.ToUpper();
        var agent = await db.Agents.FirstOrDefaultAsync(a => a.AeTitle == ae);

        if (agent is null)
        {
            agent = new Agent { AeTitle = ae, FirstSeen = DateTime.UtcNow };
            db.Agents.Add(agent);
        }

        agent.LastSeen          = DateTime.UtcNow;
        agent.InstancesReceived += body.InstancesDelta;
        await db.SaveChangesAsync();

        return Results.Ok(new { ok = true });
    }

    static async Task<IResult> OnInstanceReceived(
        RouterService router,
        IngestNotification body)
    {
        var queued = await router.EvaluateAndQueueAsync(
            body.InstanceId,
            body.Modality     ?? "",
            body.SendingAe    ?? "",
            body.ReceivingAe  ?? "",
            body.BodyPart     ?? ""
        );

        // Fire-and-forget queue processing for immediate delivery
        if (queued > 0)
            _ = Task.Run(() => router.ProcessQueueAsync());

        return Results.Ok(new { ok = true, routesQueued = queued });
    }

    static async Task<IResult> RouteInstance(
        RouterService router, string instanceUid, int destId)
    {
        // Background — return immediately, routing happens async
        _ = Task.Run(() => router.RouteInstanceAsync(instanceUid, destId));
        return Results.Ok(new { ok = true, message = $"Routing {instanceUid} queued" });
    }

    static async Task<IResult> RouteStudy(
        RouterService router, string studyUid, int destId)
    {
        _ = Task.Run(() => router.RouteStudyAsync(studyUid, destId));
        return Results.Ok(new { ok = true, message = $"Routing study {studyUid} queued" });
    }
}
