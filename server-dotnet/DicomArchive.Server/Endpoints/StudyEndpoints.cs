using DicomArchive.Server.Data;
using Microsoft.EntityFrameworkCore;

namespace DicomArchive.Server.Endpoints;

public static class StudyEndpoints
{
    public static void Map(WebApplication app)
    {
        app.MapGet("/api/studies", ListStudies);
        app.MapGet("/api/studies/{studyUid}", GetStudy);
        app.MapGet("/api/studies/{studyUid}/series", GetStudySeries);
        app.MapGet("/api/series/{seriesUid}/instances", GetSeriesInstances);
        app.MapGet("/api/instances/{instanceUid}", GetInstance);
        app.MapGet("/api/instances/{instanceUid}/file", DownloadInstance);
        app.MapGet("/api/stats", GetStats);
    }

    static async Task<IResult> ListStudies(
        ArchiveDbContext db,
        string? search = null, string? modality = null,
        string? date_from = null, string? date_to = null,
        int limit = 100, int offset = 0)
    {
        var q = db.Exams
            .Include(e => e.Patient)
            .Include(e => e.SeriesList).ThenInclude(s => s.Instances)
            .AsQueryable();

        if (!string.IsNullOrEmpty(search))
        {
            var pattern = $"%{search}%";
            q = q.Where(e =>
                EF.Functions.ILike(e.Patient.PatientId, pattern) ||
                (e.Patient.Name != null && EF.Functions.ILike(e.Patient.Name, pattern)) ||
                (e.Accession != null && EF.Functions.ILike(e.Accession, pattern)) ||
                (e.Description != null && EF.Functions.ILike(e.Description, pattern))
            );
        }
        if (!string.IsNullOrEmpty(modality))
            q = q.Where(e => e.Modality == modality.ToUpper());
        if (DateOnly.TryParse(date_from, out var df))
            q = q.Where(e => e.StudyDate >= df);
        if (DateOnly.TryParse(date_to, out var dt))
            q = q.Where(e => e.StudyDate <= dt);

        var results = await q
            .OrderByDescending(e => e.StudyDate)
            .ThenByDescending(e => e.Id)
            .Skip(offset).Take(limit)
            .Select(e => new StudySummary(
                e.Id, e.StudyUid, e.StudyDate, e.Accession,
                e.Description, e.Modality,
                e.Patient.PatientId, e.Patient.Name, e.Patient.BirthDate,
                e.SeriesList.Count,
                e.SeriesList.Sum(s => s.Instances.Count)
            ))
            .ToListAsync();

        return Results.Ok(results);
    }

    static async Task<IResult> GetStudy(ArchiveDbContext db, string studyUid)
    {
        var exam = await db.Exams
            .Include(e => e.Patient)
            .FirstOrDefaultAsync(e => e.StudyUid == studyUid);
        return exam is null ? Results.NotFound() : Results.Ok(exam);
    }

    static async Task<IResult> GetStudySeries(ArchiveDbContext db, string studyUid)
    {
        var series = await db.Series
            .Include(s => s.Instances)
            .Where(s => s.Exam.StudyUid == studyUid)
            .OrderBy(s => s.SeriesNumber)
            .Select(s => new {
                s.Id, s.SeriesUid, s.SeriesNumber, s.SeriesDate,
                s.BodyPart, s.Description, s.Laterality, s.ViewPosition,
                InstanceCount = s.Instances.Count
            })
            .ToListAsync();
        return Results.Ok(series);
    }

    static async Task<IResult> GetSeriesInstances(ArchiveDbContext db, string seriesUid)
    {
        var instances = await db.Instances
            .Where(i => i.Series.SeriesUid == seriesUid)
            .OrderBy(i => i.InstanceNumber)
            .ToListAsync();
        return Results.Ok(instances);
    }

    static async Task<IResult> GetInstance(ArchiveDbContext db, string instanceUid)
    {
        var inst = await db.Instances
            .Include(i => i.Series).ThenInclude(s => s.Exam)
                .ThenInclude(e => e.Patient)
            .FirstOrDefaultAsync(i => i.InstanceUid == instanceUid);
        return inst is null ? Results.NotFound() : Results.Ok(inst);
    }

    static async Task<IResult> DownloadInstance(
        ArchiveDbContext db,
        DicomArchive.Server.Services.StorageService storage,
        string instanceUid)
    {
        var inst = await db.Instances.FirstOrDefaultAsync(i => i.InstanceUid == instanceUid);
        if (inst is null) return Results.NotFound();

        var path = await storage.FetchToTempAsync(inst.BlobKey);
        var bytes = await File.ReadAllBytesAsync(path);
        File.Delete(path);

        return Results.File(bytes, "application/dicom", $"{instanceUid}.dcm");
    }

    static async Task<IResult> GetStats(ArchiveDbContext db)
    {
        var stats = new StatsResult(
            TotalPatients : await db.Patients.LongCountAsync(),
            TotalStudies  : await db.Exams.LongCountAsync(),
            TotalSeries   : await db.Series.LongCountAsync(),
            TotalInstances: await db.Instances.LongCountAsync(),
            TotalBytes    : await db.Instances.SumAsync(i => i.SizeBytes ?? 0),
            RoutesOk      : await db.RoutingLog.LongCountAsync(r => r.Status == "success"),
            RoutesFailed  : await db.RoutingLog.LongCountAsync(r => r.Status == "failed"),
            RoutesQueued  : await db.RoutingLog.LongCountAsync(r => r.Status == "queued")
        );
        return Results.Ok(stats);
    }
}
