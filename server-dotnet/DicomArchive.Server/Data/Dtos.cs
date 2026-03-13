namespace DicomArchive.Server.Data;

// InternalEndpoints
record AgentRegistration(string AeTitle, string Host, string? StorageBackend, string? Version);
record AgentHeartbeat(string AeTitle, long InstancesDelta);
record IngestNotification(int InstanceId, string? InstanceUid, string? Modality, string? SendingAe, string? ReceivingAe, string? BodyPart);

// AgentEndpoints
record AgentUpdate(string? Description, bool? Enabled);

// DestinationEndpoints
record DestinationIn(string Name, string AeTitle, string Host, int Port, string? Description, bool Enabled);

// RuleEndpoints
record RuleIn(string Name, int Priority, bool Enabled, string? MatchModality, string? MatchAeTitle, string? MatchReceivingAe, string? MatchBodyPart, bool OnReceive, string? Description, List<int> DestinationIds);

// StudyEndpoints
record StudySummary(int Id, string StudyUid, DateOnly? StudyDate, string? Accession, string? Description, string? Modality, string PatientId, string? PatientName, DateOnly? BirthDate, int SeriesCount, int InstanceCount);
record StatsResult(long TotalPatients, long TotalStudies, long TotalSeries, long TotalInstances, long TotalBytes, long RoutesOk, long RoutesFailed, long RoutesQueued);
