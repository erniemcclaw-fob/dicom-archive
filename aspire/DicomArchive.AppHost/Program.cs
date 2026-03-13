var builder = DistributedApplication.CreateBuilder(args);

// ── Postgres ──────────────────────────────────────────────────────────────────
var postgres = builder.AddPostgres("postgres")
    .WithDataVolume("dicom-pgdata")
    .WithPgAdmin()
    .AddDatabase("dicom-archive");

// ── Seq (structured logging) ─────────────────────────────────────────────────
var seq = builder.AddSeq("seq")
    .ExcludeFromManifest();

// ── .NET Server ───────────────────────────────────────────────────────────────
var server = builder.AddProject<Projects.DicomArchive_Server>("dicom-server")
    .WithReference(postgres)
    .WaitFor(postgres)
    .WithReference(seq)
    .WithHttpEndpoint(port: 8080, name: "web");

// ── Python Ingest Agent ───────────────────────────────────────────────────────
// WithReference(postgres) injects ConnectionStrings__dicom-archive in ADO.NET
// format (Host=...;Database=...;Username=...;Password=...).
// The Python agent reads this and converts it to a psycopg2 URL — see database.py.
builder.AddDockerfile("dicom-agent", "../../agent")
    .WithReference(postgres)
    .WaitFor(postgres)
    .WithEnvironment("STORAGE_BACKEND",    "local")
    .WithEnvironment("LOCAL_STORAGE_PATH", "/data/received")
    .WithEnvironment("AE_TITLE",           "ARCHIVE_SCP")
    .WithEnvironment("LISTEN_PORT",        "11112")
    .WithEnvironment("ROUTER_URL",         server.GetEndpoint("web"))
    .WithEnvironment("SEQ_URL",            seq.GetEndpoint("http"))
    .WithBindMount("../../data/received",   "/data/received")
    .WithBindMount("../../data/quarantine", "/data/quarantine")
    .WithEndpoint(port: 11112, targetPort: 11112, scheme: "tcp", name: "dicom")
    .WaitFor(server);

builder.Build().Run();
