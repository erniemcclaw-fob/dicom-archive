using Microsoft.EntityFrameworkCore;

namespace DicomArchive.Server.Data;

public class ArchiveDbContext(DbContextOptions<ArchiveDbContext> options) : DbContext(options)
{
    public DbSet<Patient> Patients => Set<Patient>();
    public DbSet<Exam> Exams => Set<Exam>();
    public DbSet<Series> Series => Set<Series>();
    public DbSet<Instance> Instances => Set<Instance>();
    public DbSet<AeDestination> AeDestinations => Set<AeDestination>();
    public DbSet<RoutingRule> RoutingRules => Set<RoutingRule>();
    public DbSet<RuleDestination> RuleDestinations => Set<RuleDestination>();
    public DbSet<RoutingLogEntry> RoutingLog => Set<RoutingLogEntry>();
    public DbSet<Agent> Agents => Set<Agent>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        // ── patients ──
        modelBuilder.Entity<Patient>(e =>
        {
            e.ToTable("patients");
            e.Property(p => p.Id).HasColumnName("id");
            e.Property(p => p.PatientId).HasColumnName("patient_id");
            e.Property(p => p.Name).HasColumnName("name");
            e.Property(p => p.BirthDate).HasColumnName("birth_date");
            e.Property(p => p.Sex).HasColumnName("sex");
            e.Property(p => p.CreatedAt).HasColumnName("created_at");
        });

        // ── exams ──
        modelBuilder.Entity<Exam>(e =>
        {
            e.ToTable("exams");
            e.Property(x => x.Id).HasColumnName("id");
            e.Property(x => x.PatientId).HasColumnName("patient_id");
            e.Property(x => x.StudyUid).HasColumnName("study_uid");
            e.Property(x => x.StudyDate).HasColumnName("study_date");
            e.Property(x => x.StudyTime).HasColumnName("study_time");
            e.Property(x => x.Accession).HasColumnName("accession");
            e.Property(x => x.Description).HasColumnName("description");
            e.Property(x => x.Modality).HasColumnName("modality");
            e.Property(x => x.ReferringPhysician).HasColumnName("referring_physician");
            e.Property(x => x.CreatedAt).HasColumnName("created_at");
            e.HasOne(x => x.Patient).WithMany(p => p.Exams).HasForeignKey(x => x.PatientId);
        });

        // ── series ──
        modelBuilder.Entity<Series>(e =>
        {
            e.ToTable("series");
            e.Property(s => s.Id).HasColumnName("id");
            e.Property(s => s.ExamId).HasColumnName("exam_id");
            e.Property(s => s.SeriesUid).HasColumnName("series_uid");
            e.Property(s => s.SeriesNumber).HasColumnName("series_number");
            e.Property(s => s.SeriesDate).HasColumnName("series_date");
            e.Property(s => s.BodyPart).HasColumnName("body_part");
            e.Property(s => s.Description).HasColumnName("description");
            e.Property(s => s.Laterality).HasColumnName("laterality");
            e.Property(s => s.ViewPosition).HasColumnName("view_position");
            e.Property(s => s.CreatedAt).HasColumnName("created_at");
            e.HasOne(s => s.Exam).WithMany(x => x.SeriesList).HasForeignKey(s => s.ExamId);
        });

        // ── instances ──
        modelBuilder.Entity<Instance>(e =>
        {
            e.ToTable("instances");
            e.Property(i => i.Id).HasColumnName("id");
            e.Property(i => i.SeriesId).HasColumnName("series_id");
            e.Property(i => i.InstanceUid).HasColumnName("instance_uid");
            e.Property(i => i.InstanceNumber).HasColumnName("instance_number");
            e.Property(i => i.BlobKey).HasColumnName("blob_key");
            e.Property(i => i.BlobUri).HasColumnName("blob_uri");
            e.Property(i => i.SizeBytes).HasColumnName("size_bytes");
            e.Property(i => i.Sha256).HasColumnName("sha256");
            e.Property(i => i.TransferSyntax).HasColumnName("transfer_syntax");
            e.Property(i => i.Rows).HasColumnName("rows");
            e.Property(i => i.Columns).HasColumnName("columns");
            e.Property(i => i.ReceivedAt).HasColumnName("received_at");
            e.Property(i => i.SendingAe).HasColumnName("sending_ae");
            e.Property(i => i.ReceivingAe).HasColumnName("receiving_ae");
            e.HasOne(i => i.Series).WithMany(s => s.Instances).HasForeignKey(i => i.SeriesId);
        });

        // ── ae_destinations ──
        modelBuilder.Entity<AeDestination>(e =>
        {
            e.ToTable("ae_destinations");
            e.Property(d => d.Id).HasColumnName("id");
            e.Property(d => d.Name).HasColumnName("name");
            e.Property(d => d.AeTitle).HasColumnName("ae_title");
            e.Property(d => d.Host).HasColumnName("host");
            e.Property(d => d.Port).HasColumnName("port");
            e.Property(d => d.Description).HasColumnName("description");
            e.Property(d => d.Enabled).HasColumnName("enabled");
            e.Property(d => d.CreatedAt).HasColumnName("created_at");
            e.Property(d => d.UpdatedAt).HasColumnName("updated_at");
        });

        // ── routing_rules ──
        modelBuilder.Entity<RoutingRule>(e =>
        {
            e.ToTable("routing_rules");
            e.Property(r => r.Id).HasColumnName("id");
            e.Property(r => r.Name).HasColumnName("name");
            e.Property(r => r.Priority).HasColumnName("priority");
            e.Property(r => r.Enabled).HasColumnName("enabled");
            e.Property(r => r.MatchModality).HasColumnName("match_modality");
            e.Property(r => r.MatchAeTitle).HasColumnName("match_ae_title");
            e.Property(r => r.MatchReceivingAe).HasColumnName("match_receiving_ae");
            e.Property(r => r.MatchBodyPart).HasColumnName("match_body_part");
            e.Property(r => r.OnReceive).HasColumnName("on_receive");
            e.Property(r => r.Description).HasColumnName("description");
            e.Property(r => r.CreatedAt).HasColumnName("created_at");
            e.Property(r => r.UpdatedAt).HasColumnName("updated_at");
        });

        // ── rule_destinations ──
        modelBuilder.Entity<RuleDestination>(e =>
        {
            e.ToTable("rule_destinations");
            e.HasKey(rd => new { rd.RuleId, rd.DestinationId });
            e.Property(rd => rd.RuleId).HasColumnName("rule_id");
            e.Property(rd => rd.DestinationId).HasColumnName("destination_id");
            e.HasOne(rd => rd.Rule).WithMany(r => r.RuleDestinations).HasForeignKey(rd => rd.RuleId);
            e.HasOne(rd => rd.Destination).WithMany().HasForeignKey(rd => rd.DestinationId);
        });

        // ── routing_log ──
        modelBuilder.Entity<RoutingLogEntry>(e =>
        {
            e.ToTable("routing_log");
            e.Property(rl => rl.Id).HasColumnName("id");
            e.Property(rl => rl.InstanceId).HasColumnName("instance_id");
            e.Property(rl => rl.RuleId).HasColumnName("rule_id");
            e.Property(rl => rl.DestinationId).HasColumnName("destination_id");
            e.Property(rl => rl.Status).HasColumnName("status");
            e.Property(rl => rl.Attempts).HasColumnName("attempts");
            e.Property(rl => rl.LastError).HasColumnName("last_error");
            e.Property(rl => rl.QueuedAt).HasColumnName("queued_at");
            e.Property(rl => rl.SentAt).HasColumnName("sent_at");
            e.HasOne(rl => rl.Instance).WithMany().HasForeignKey(rl => rl.InstanceId);
            e.HasOne(rl => rl.Rule).WithMany().HasForeignKey(rl => rl.RuleId);
            e.HasOne(rl => rl.Destination).WithMany().HasForeignKey(rl => rl.DestinationId);
        });

        // ── agents ──
        modelBuilder.Entity<Agent>(e =>
        {
            e.ToTable("agents");
            e.Property(a => a.Id).HasColumnName("id");
            e.Property(a => a.AeTitle).HasColumnName("ae_title");
            e.Property(a => a.Host).HasColumnName("host");
            e.Property(a => a.Description).HasColumnName("description");
            e.Property(a => a.Enabled).HasColumnName("enabled");
            e.Property(a => a.StorageBackend).HasColumnName("storage_backend");
            e.Property(a => a.Version).HasColumnName("version");
            e.Property(a => a.FirstSeen).HasColumnName("first_seen");
            e.Property(a => a.LastSeen).HasColumnName("last_seen");
            e.Property(a => a.InstancesReceived).HasColumnName("instances_received");
        });
    }
}
