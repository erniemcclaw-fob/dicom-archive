var builder = DistributedApplication.CreateBuilder(args);

// ── Shared secret for agent ↔ server auth ───────────────────────────────────
// In production, use a proper secret store. For local dev, generate one.
var agentApiKey = builder.AddParameter("agent-api-key", secret: true);

// ── Postgres ──────────────────────────────────────────────────────────────────
var postgres = builder.AddPostgres("postgres")
    .WithPgAdmin()
    .AddDatabase("dicom-archive");

// ── Azure Storage (Azurite emulator for local dev) ─────────────────────────
var storage = builder.AddAzureStorage("azure-storage")
    .RunAsEmulator(emulator =>
        emulator.WithArgs("--disableProductStyleUrl"));  // path-style URLs for Docker networking
var blobs = storage.AddBlobs("blobs");

// ── Seq (structured logging) ─────────────────────────────────────────────────
var seq = builder.AddSeq("seq")
    .ExcludeFromManifest();                 // ephemeral — no volume, resets each run

// ── .NET Server ───────────────────────────────────────────────────────────────
var server = builder.AddProject<Projects.DicomArchive_Server>("dicom-server")
    .WithReference(postgres)
    .WaitFor(postgres)
    .WithReference(blobs)
    .WaitFor(storage)
    .WithReference(seq)
    .WithEnvironment("STORAGE_BACKEND",  "azure")
    .WithEnvironment("AZURE_CONTAINER",  "dicom-files")
    .WithEnvironment("AGENT_API_KEY",    agentApiKey)
    .WithHttpEndpoint(port: 8080, name: "web");

// ── Python Ingest Agent ───────────────────────────────────────────────────────
// The agent has NO database access and NO cloud storage credentials.
// It communicates with the server via the 3-step ingest handshake and
// uploads files directly to blob storage using pre-signed URLs from the server.
builder.AddDockerfile("dicom-agent", "../../agent")
    .WaitFor(server)
    .WaitFor(storage)
    .WithEnvironment("AE_TITLE",           "ARCHIVE_SCP")
    .WithEnvironment("LISTEN_PORT",        "11112")
    .WithEnvironment("SERVER_URL",         server.GetEndpoint("web"))
    .WithEnvironment("AGENT_API_KEY",      agentApiKey)
    .WithEnvironment("UPLOAD_WORKERS",     "4")
    .WithEnvironment("STAGING_PATH",       "/data/staging")
    .WithEnvironment("SEQ_URL",            seq.GetEndpoint("http"))
    .WithBindMount("../../data/staging",    "/data/staging")
    .WithBindMount("../../data/quarantine", "/data/quarantine")
    .WithEndpoint(port: 11112, targetPort: 11112, scheme: "tcp", name: "dicom");

builder.Build().Run();
