using Azure.Storage.Blobs;

namespace DicomArchive.Server.Services;

/// <summary>
/// Retrieves DICOM blobs from the configured storage backend into a local
/// temp file for routing. Mirrors the logic in agent/storage.py.
/// </summary>
public class StorageService(IConfiguration config, ILogger<StorageService> logger)
{
    private readonly string _backend = config["STORAGE_BACKEND"] ?? "local";
    private readonly string _localBase = config["LOCAL_STORAGE_PATH"] ?? "./received";
    private readonly string _azureContainer = config["AZURE_CONTAINER"] ?? "dicom-files";
    private BlobContainerClient? _blobContainer;

    /// <summary>
    /// Generates a time-limited, path-scoped upload URL (SAS token for Azure,
    /// pre-signed URL for S3) that the edge agent uses to PUT the .dcm directly.
    /// </summary>
    public (string Url, string ExpiresAt) GenerateUploadUrl(string blobKey)
    {
        switch (_backend.ToLower())
        {
            case "azure":
                return GenerateAzureUploadUrl(blobKey);
            case "s3":
                throw new NotImplementedException(
                    "S3 pre-signed upload URLs: add AWSSDK.S3 and implement GenerateS3UploadUrl.");
            case "local":
                // Local storage: return a server-relative URL; the agent will PUT to our server.
                // In production, use Azure or S3 for direct uploads.
                var expiry = DateTime.UtcNow.AddMinutes(30);
                return ($"/blobs/{blobKey}", expiry.ToString("o"));
            default:
                throw new InvalidOperationException($"Unknown STORAGE_BACKEND: {_backend}");
        }
    }

    private (string Url, string ExpiresAt) GenerateAzureUploadUrl(string blobKey)
    {
        var container = GetBlobContainer();
        var blobClient = container.GetBlobClient(blobKey);

        var expiry = DateTimeOffset.UtcNow.AddMinutes(30);
        var sasBuilder = new Azure.Storage.Sas.BlobSasBuilder
        {
            BlobContainerName = container.Name,
            BlobName          = blobKey,
            Resource          = "b",    // blob-level
            ExpiresOn         = expiry,
        };
        sasBuilder.SetPermissions(Azure.Storage.Sas.BlobSasPermissions.Create | Azure.Storage.Sas.BlobSasPermissions.Write);

        var sasUri = blobClient.GenerateSasUri(sasBuilder);
        return (sasUri.ToString(), expiry.UtcDateTime.ToString("o"));
    }

    /// <summary>
    /// Stores a blob from a request body stream (server-proxied upload).
    /// The agent PUTs the .dcm file to /ingest/upload/{id} and this method
    /// writes it to the configured storage backend.
    /// </summary>
    public async Task StoreFromStreamAsync(string blobKey, Stream body)
    {
        switch (_backend.ToLower())
        {
            case "local":
                var dest = Path.Combine(_localBase, blobKey.Replace('/', Path.DirectorySeparatorChar));
                Directory.CreateDirectory(Path.GetDirectoryName(dest)!);
                await using (var fs = File.Create(dest))
                    await body.CopyToAsync(fs);
                logger.LogDebug("Stored blob {Key} → {Path}", blobKey, dest);
                break;

            case "azure":
                var container = GetBlobContainer();
                var blobClient = container.GetBlobClient(blobKey);
                await blobClient.UploadAsync(body, overwrite: true);
                logger.LogDebug("Stored blob {Key} → Azure", blobKey);
                break;

            case "s3":
                throw new NotImplementedException("S3 upload not yet implemented.");

            default:
                throw new InvalidOperationException($"Unknown STORAGE_BACKEND: {_backend}");
        }
    }

    public async Task<string> FetchToTempAsync(string blobKey)
    {
        var tmp = Path.Combine(Path.GetTempPath(), $"dcm_{Guid.NewGuid():N}.dcm");

        switch (_backend.ToLower())
        {
            case "local":
                await FetchLocalAsync(blobKey, tmp);
                break;
            case "s3":
                await FetchS3Async(blobKey, tmp);
                break;
            case "azure":
                await FetchAzureAsync(blobKey, tmp);
                break;
            default:
                throw new InvalidOperationException($"Unknown STORAGE_BACKEND: {_backend}");
        }

        logger.LogDebug("Fetched blob {Key} → {Tmp}", blobKey, tmp);
        return tmp;
    }

    private Task FetchLocalAsync(string blobKey, string dest)
    {
        var src = Path.Combine(_localBase, blobKey.Replace('/', Path.DirectorySeparatorChar));
        if (!File.Exists(src))
            throw new FileNotFoundException($"Blob not found: {src}");
        File.Copy(src, dest, overwrite: true);
        return Task.CompletedTask;
    }

    private Task FetchS3Async(string blobKey, string dest)
    {
        // S3 support requires adding AWSSDK.S3 package and uncommenting this implementation.
        // See INSTALL.md § Cloud Storage for setup instructions.
        throw new NotImplementedException(
            "S3 storage: add PackageReference for AWSSDK.S3 to DicomArchive.Server.csproj " +
            "then implement FetchS3Async using AmazonS3Client.");
    }

    private async Task FetchAzureAsync(string blobKey, string dest)
    {
        var container = GetBlobContainer();
        var blobClient = container.GetBlobClient(blobKey);

        if (!await blobClient.ExistsAsync())
            throw new FileNotFoundException($"Azure blob not found: {blobKey}");

        await using var stream = File.OpenWrite(dest);
        await blobClient.DownloadToAsync(stream);
    }

    private BlobContainerClient GetBlobContainer()
    {
        if (_blobContainer is not null)
            return _blobContainer;

        // Aspire injects "ConnectionStrings:blobs"; fall back to env var
        var connectionString = config.GetConnectionString("blobs")
            ?? config["AZURE_STORAGE_CONNECTION_STRING"]
            ?? throw new InvalidOperationException(
                "Azure storage requires ConnectionStrings:blobs or AZURE_STORAGE_CONNECTION_STRING");

        // Strip ";ContainerName=..." appended by Aspire blob resource references
        connectionString = string.Join(';',
            connectionString.Split(';')
                .Where(p => !p.StartsWith("ContainerName=", StringComparison.OrdinalIgnoreCase)));

        var serviceClient = new BlobServiceClient(connectionString);
        _blobContainer = serviceClient.GetBlobContainerClient(_azureContainer);
        _blobContainer.CreateIfNotExists();
        return _blobContainer;
    }
}
