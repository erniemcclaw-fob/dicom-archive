using System.Data;
using DicomArchive.Server.Data;
using Microsoft.EntityFrameworkCore.Storage;
using DicomArchive.Server.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;

namespace DicomArchive.Server.Endpoints;

/// <summary>
/// 3-step ingest handshake endpoints used by edge agents.
/// Replaces the old /internal/* endpoints. All calls require X-Api-Key auth.
/// </summary>
public static class IngestEndpoints
{
    public static void Map(WebApplication app)
    {
        var group = app.MapGroup("/ingest")
            .RequireAuthorization("AgentPolicy");

        group.MapPost("/prepare", Prepare);
        group.MapPut("/upload/{instanceId:int}", Upload).DisableAntiforgery();
        group.MapPost("/confirm", Confirm);

        // Keep register/heartbeat under /ingest — agents need these too
        group.MapPost("/register",  Register);
        group.MapPost("/heartbeat", Heartbeat);

        // Manual routing (used by UI, does not require agent auth)
        app.MapPost("/api/route/instance/{instanceUid}/to/{destId:int}", RouteInstance);
        app.MapPost("/api/route/study/{studyUid}/to/{destId:int}",       RouteStudy);
    }

    /// <summary>
    /// Executes an INSERT ... ON CONFLICT ... RETURNING id and returns the id.
    /// Uses raw ADO.NET because EF Core's SqlQueryRaw cannot compose over
    /// non-composable SQL (INSERT/UPDATE with RETURNING).
    /// </summary>
    private static async Task<int> UpsertReturningIdAsync(ArchiveDbContext db, string sql, params object?[] parameters)
    {
        var conn = db.Database.GetDbConnection();
        if (conn.State != ConnectionState.Open)
            await conn.OpenAsync();

        await using var cmd = conn.CreateCommand();
        cmd.CommandText = sql;
        cmd.Transaction = db.Database.CurrentTransaction?.GetDbTransaction();

        for (int i = 0; i < parameters.Length; i++)
        {
            var p = cmd.CreateParameter();
            p.ParameterName = $"p{i}";
            p.Value = parameters[i] ?? DBNull.Value;
            cmd.Parameters.Add(p);
        }

        var result = await cmd.ExecuteScalarAsync();
        return Convert.ToInt32(result);
    }

    // ── Step 1: Prepare ────────────────────────────────────────────────────────

    static async Task<IResult> Prepare(
        ArchiveDbContext db,
        HttpRequest request,
        ILogger<Program> logger,
        [FromBody] PrepareRequest body)
    {
        if (string.IsNullOrEmpty(body.InstanceUid))
            return Results.BadRequest(new { ok = false, error = "Missing instance_uid" });

        // Use raw SQL upserts — safe under concurrent requests from parallel workers.

        // ── Upsert patient ──
        var patientId = await UpsertReturningIdAsync(db, """
            INSERT INTO patients (patient_id, name, created_at)
            VALUES (@p0, @p1, NOW())
            ON CONFLICT (patient_id) DO UPDATE
                SET name = COALESCE(EXCLUDED.name, patients.name)
            RETURNING id
            """, body.PatientId ?? "UNKNOWN", body.PatientName);

        // ── Upsert exam ──
        var examId = await UpsertReturningIdAsync(db, """
            INSERT INTO exams (patient_id, study_uid, study_date, modality, created_at)
            VALUES (@p0, @p1, @p2, @p3, NOW())
            ON CONFLICT (study_uid) DO UPDATE
                SET study_date = COALESCE(EXCLUDED.study_date, exams.study_date)
            RETURNING id
            """, patientId, body.StudyUid ?? "",
                 (object?)ParseDate(body.StudyDate) ?? DBNull.Value,
                 body.Modality);

        // ── Upsert series ──
        var seriesId = await UpsertReturningIdAsync(db, """
            INSERT INTO series (exam_id, series_uid, body_part, created_at)
            VALUES (@p0, @p1, @p2, NOW())
            ON CONFLICT (series_uid) DO UPDATE
                SET body_part = COALESCE(EXCLUDED.body_part, series.body_part)
            RETURNING id
            """, examId, body.SeriesUid ?? "", body.BodyPart);

        // ── Build blob key ──
        var studyDate = body.StudyDate ?? "UNKNOWN";
        var blobKey = $"{studyDate}/{body.StudyUid}/{body.SeriesUid}/{body.InstanceUid}.dcm";

        // ── Upsert instance (status=pending) ──
        var instanceId = await UpsertReturningIdAsync(db, """
            INSERT INTO instances (series_id, instance_uid, blob_key, size_bytes, sha256,
                                   sending_ae, receiving_ae, received_at, status)
            VALUES (@p0, @p1, @p2, @p3, @p4, @p5, @p6, NOW(), 'pending')
            ON CONFLICT (instance_uid) DO UPDATE
                SET status = 'pending', sha256 = EXCLUDED.sha256, blob_key = EXCLUDED.blob_key
            RETURNING id
            """, seriesId, body.InstanceUid, blobKey,
                 (object?)body.FileSizeBytes ?? DBNull.Value,
                 body.Sha256,
                 body.SendingAe,
                 body.AeTitle);

        // ── Build upload URL ──
        // Return a server-proxied upload URL. The agent PUTs the file here; the server
        // writes it to blob storage. This avoids Docker networking issues with SAS URLs
        // and works with any storage backend (local, Azure, S3).
        var scheme = request.Scheme;
        var host = request.Host;
        var uploadUrl = $"{scheme}://{host}/ingest/upload/{instanceId}";
        var expiresAt = DateTime.UtcNow.AddMinutes(30).ToString("o");

        logger.LogInformation("Prepare: instance {Uid} → blob {Key}", body.InstanceUid, blobKey);

        return Results.Ok(new
        {
            ok                    = true,
            instance_id           = instanceId,
            blob_key              = blobKey,
            upload_url            = uploadUrl,
            upload_url_expires_at = expiresAt,
        });
    }

    // ── Step 2: Upload (server-proxied) ────────────────────────────────────────

    static async Task<IResult> Upload(
        ArchiveDbContext db,
        StorageService storage,
        ILogger<Program> logger,
        int instanceId,
        HttpRequest request)
    {
        var instance = await db.Instances.FindAsync(instanceId);
        if (instance is null)
            return Results.NotFound(new { ok = false, error = "Instance not found" });

        if (string.IsNullOrEmpty(instance.BlobKey))
            return Results.BadRequest(new { ok = false, error = "Instance has no blob_key — call /prepare first" });

        await storage.StoreFromStreamAsync(instance.BlobKey, request.Body);

        logger.LogInformation("Upload: instance {Id} blob written to {Key}", instanceId, instance.BlobKey);

        return Results.Ok(new { ok = true });
    }

    // ── Step 3: Confirm ────────────────────────────────────────────────────────

    static async Task<IResult> Confirm(
        ArchiveDbContext db,
        RouterService router,
        ILogger<Program> logger,
        [FromBody] ConfirmRequest body)
    {
        var instance = await db.Instances
            .Include(i => i.Series)
            .ThenInclude(s => s.Exam)
            .FirstOrDefaultAsync(i => i.Id == body.InstanceId);

        if (instance is null)
            return Results.NotFound(new { ok = false, error = "Instance not found" });

        // Verify SHA-256 matches what was declared at prepare time
        if (!string.IsNullOrEmpty(body.Sha256) &&
            !string.IsNullOrEmpty(instance.Sha256) &&
            !string.Equals(instance.Sha256, body.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            return Results.BadRequest(new { ok = false, error = "SHA-256 mismatch" });
        }

        instance.Status = "stored";
        await db.SaveChangesAsync();

        // ── Trigger routing engine ──
        var queued = await router.EvaluateAndQueueAsync(
            instance.Id,
            instance.Series?.Exam?.Modality ?? "",
            instance.SendingAe             ?? "",
            instance.ReceivingAe           ?? "",
            instance.Series?.BodyPart      ?? ""
        );

        if (queued > 0)
            _ = Task.Run(() => router.ProcessQueueAsync());

        logger.LogInformation("Confirm: instance {Id} stored, {Routes} route(s) queued",
            instance.Id, queued);

        return Results.Ok(new { ok = true, routes_queued = queued });
    }

    // ── Register / Heartbeat ───────────────────────────────────────────────────

    static async Task<IResult> Register(ArchiveDbContext db, [FromBody] AgentRegistration? body,
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

    static async Task<IResult> Heartbeat(ArchiveDbContext db, [FromBody] AgentHeartbeat? body)
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

    // ── Manual routing (unchanged, no auth required) ───────────────────────────

    static async Task<IResult> RouteInstance(
        RouterService router, string instanceUid, int destId)
    {
        _ = Task.Run(() => router.RouteInstanceAsync(instanceUid, destId));
        return Results.Ok(new { ok = true, message = $"Routing {instanceUid} queued" });
    }

    static async Task<IResult> RouteStudy(
        RouterService router, string studyUid, int destId)
    {
        _ = Task.Run(() => router.RouteStudyAsync(studyUid, destId));
        return Results.Ok(new { ok = true, message = $"Routing study {studyUid} queued" });
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    private static DateOnly? ParseDate(string? val)
    {
        if (string.IsNullOrEmpty(val) || val.Length != 8) return null;
        if (int.TryParse(val[..4], out var y) &&
            int.TryParse(val[4..6], out var m) &&
            int.TryParse(val[6..8], out var d))
        {
            try { return new DateOnly(y, m, d); }
            catch { return null; }
        }
        return null;
    }
}

// ── Request DTOs ──────────────────────────────────────────────────────────────

public record PrepareRequest
{
    public string? AeTitle { get; init; }
    public string? SendingAe { get; init; }
    public string? PatientId { get; init; }
    public string? PatientName { get; init; }
    public string? StudyUid { get; init; }
    public string? StudyDate { get; init; }
    public string? SeriesUid { get; init; }
    public string? InstanceUid { get; init; }
    public string? Modality { get; init; }
    public string? BodyPart { get; init; }
    public long? FileSizeBytes { get; init; }
    public string? Sha256 { get; init; }
}

public record ConfirmRequest
{
    public int InstanceId { get; init; }
    public string? Sha256 { get; init; }
}
