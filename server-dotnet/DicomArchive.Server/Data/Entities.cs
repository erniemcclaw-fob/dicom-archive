namespace DicomArchive.Server.Data;

public class Patient
{
    public int Id { get; set; }
    public string PatientId { get; set; } = "";
    public string? Name { get; set; }
    public DateOnly? BirthDate { get; set; }
    public string? Sex { get; set; }
    public DateTime? CreatedAt { get; set; }

    public List<Exam> Exams { get; set; } = [];
}

public class Exam
{
    public int Id { get; set; }
    public int PatientId { get; set; }
    public string StudyUid { get; set; } = "";
    public DateOnly? StudyDate { get; set; }
    public string? StudyTime { get; set; }
    public string? Accession { get; set; }
    public string? Description { get; set; }
    public string? Modality { get; set; }
    public string? ReferringPhysician { get; set; }
    public DateTime? CreatedAt { get; set; }

    public Patient Patient { get; set; } = null!;
    public List<Series> SeriesList { get; set; } = [];
}

public class Series
{
    public int Id { get; set; }
    public int ExamId { get; set; }
    public string SeriesUid { get; set; } = "";
    public int? SeriesNumber { get; set; }
    public DateOnly? SeriesDate { get; set; }
    public string? BodyPart { get; set; }
    public string? Description { get; set; }
    public string? Laterality { get; set; }
    public string? ViewPosition { get; set; }
    public DateTime? CreatedAt { get; set; }

    public Exam Exam { get; set; } = null!;
    public List<Instance> Instances { get; set; } = [];
}

public class Instance
{
    public int Id { get; set; }
    public int SeriesId { get; set; }
    public string InstanceUid { get; set; } = "";
    public int? InstanceNumber { get; set; }
    public string BlobKey { get; set; } = "";
    public string? BlobUri { get; set; }
    public long? SizeBytes { get; set; }
    public string? Sha256 { get; set; }
    public string? TransferSyntax { get; set; }
    public int? Rows { get; set; }
    public int? Columns { get; set; }
    public DateTime? ReceivedAt { get; set; }
    public string? SendingAe { get; set; }
    public string? ReceivingAe { get; set; }
    public string Status { get; set; } = "stored";

    public Series Series { get; set; } = null!;
}

public class AeDestination
{
    public int Id { get; set; }
    public string Name { get; set; } = "";
    public string AeTitle { get; set; } = "";
    public string Host { get; set; } = "";
    public int Port { get; set; } = 104;
    public string? Description { get; set; }
    public bool Enabled { get; set; } = true;
    public DateTime? CreatedAt { get; set; }
    public DateTime? UpdatedAt { get; set; }
}

public class RoutingRule
{
    public int Id { get; set; }
    public string Name { get; set; } = "";
    public int Priority { get; set; } = 100;
    public bool Enabled { get; set; } = true;
    public string? MatchModality { get; set; }
    public string? MatchAeTitle { get; set; }
    public string? MatchReceivingAe { get; set; }
    public string? MatchBodyPart { get; set; }
    public bool OnReceive { get; set; }
    public string? Description { get; set; }
    public DateTime? CreatedAt { get; set; }
    public DateTime? UpdatedAt { get; set; }

    public List<RuleDestination> RuleDestinations { get; set; } = [];
}

public class RuleDestination
{
    public int RuleId { get; set; }
    public int DestinationId { get; set; }

    public RoutingRule Rule { get; set; } = null!;
    public AeDestination Destination { get; set; } = null!;
}

public class RoutingLogEntry
{
    public int Id { get; set; }
    public int? InstanceId { get; set; }
    public int? RuleId { get; set; }
    public int? DestinationId { get; set; }
    public string Status { get; set; } = "queued";
    public int Attempts { get; set; }
    public string? LastError { get; set; }
    public DateTime? QueuedAt { get; set; }
    public DateTime? SentAt { get; set; }

    public Instance? Instance { get; set; }
    public RoutingRule? Rule { get; set; }
    public AeDestination? Destination { get; set; }
}

public class Agent
{
    public int Id { get; set; }
    public string AeTitle { get; set; } = "";
    public string? Host { get; set; }
    public string? Description { get; set; }
    public bool Enabled { get; set; } = true;
    public string? StorageBackend { get; set; }
    public string? Version { get; set; }
    public DateTime? FirstSeen { get; set; }
    public DateTime? LastSeen { get; set; }
    public long InstancesReceived { get; set; }
}
