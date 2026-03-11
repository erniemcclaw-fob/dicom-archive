using Microsoft.EntityFrameworkCore;

namespace DicomArchive.Server.Data;

/// <summary>
/// Runs the DDL to create all tables on startup if they don't already exist.
/// Mirrors the schema in agent/database.py — all CREATE TABLE IF NOT EXISTS,
/// so it is safe to run against an existing database.
/// </summary>
public static class SchemaInitializer
{
    private const string Ddl = """
        CREATE TABLE IF NOT EXISTS patients (
            id          SERIAL PRIMARY KEY,
            patient_id  TEXT NOT NULL,
            name        TEXT,
            birth_date  DATE,
            sex         CHAR(1),
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (patient_id)
        );

        CREATE TABLE IF NOT EXISTS exams (
            id                  SERIAL PRIMARY KEY,
            patient_id          INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
            study_uid           TEXT NOT NULL,
            study_date          DATE,
            study_time          TEXT,
            accession           TEXT,
            description         TEXT,
            modality            TEXT,
            referring_physician TEXT,
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (study_uid)
        );

        CREATE TABLE IF NOT EXISTS series (
            id            SERIAL PRIMARY KEY,
            exam_id       INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
            series_uid    TEXT NOT NULL,
            series_number INTEGER,
            series_date   DATE,
            body_part     TEXT,
            description   TEXT,
            laterality    TEXT,
            view_position TEXT,
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (series_uid)
        );

        CREATE TABLE IF NOT EXISTS instances (
            id              SERIAL PRIMARY KEY,
            series_id       INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
            instance_uid    TEXT NOT NULL,
            instance_number INTEGER,
            blob_key        TEXT NOT NULL,
            blob_uri        TEXT,
            size_bytes      BIGINT,
            sha256          TEXT,
            transfer_syntax TEXT,
            rows            INTEGER,
            columns         INTEGER,
            received_at     TIMESTAMPTZ DEFAULT NOW(),
            sending_ae      TEXT,
            receiving_ae    TEXT,
            UNIQUE (instance_uid)
        );

        CREATE TABLE IF NOT EXISTS ae_destinations (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            ae_title    TEXT NOT NULL,
            host        TEXT NOT NULL,
            port        INTEGER NOT NULL DEFAULT 104,
            description TEXT,
            enabled     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (name)
        );

        CREATE TABLE IF NOT EXISTS routing_rules (
            id                  SERIAL PRIMARY KEY,
            name                TEXT NOT NULL,
            priority            INTEGER NOT NULL DEFAULT 100,
            enabled             BOOLEAN NOT NULL DEFAULT TRUE,
            match_modality      TEXT,
            match_ae_title      TEXT,
            match_receiving_ae  TEXT,
            match_body_part     TEXT,
            on_receive          BOOLEAN NOT NULL DEFAULT FALSE,
            description         TEXT,
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            updated_at          TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS rule_destinations (
            rule_id        INTEGER NOT NULL REFERENCES routing_rules(id) ON DELETE CASCADE,
            destination_id INTEGER NOT NULL REFERENCES ae_destinations(id) ON DELETE CASCADE,
            PRIMARY KEY (rule_id, destination_id)
        );

        CREATE TABLE IF NOT EXISTS routing_log (
            id             SERIAL PRIMARY KEY,
            instance_id    INTEGER REFERENCES instances(id) ON DELETE SET NULL,
            rule_id        INTEGER REFERENCES routing_rules(id) ON DELETE SET NULL,
            destination_id INTEGER REFERENCES ae_destinations(id) ON DELETE SET NULL,
            status         TEXT NOT NULL DEFAULT 'queued',
            attempts       INTEGER NOT NULL DEFAULT 0,
            last_error     TEXT,
            queued_at      TIMESTAMPTZ DEFAULT NOW(),
            sent_at        TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS agents (
            id                 SERIAL PRIMARY KEY,
            ae_title           TEXT NOT NULL,
            host               TEXT,
            description        TEXT,
            enabled            BOOLEAN NOT NULL DEFAULT TRUE,
            storage_backend    TEXT,
            version            TEXT,
            first_seen         TIMESTAMPTZ DEFAULT NOW(),
            last_seen          TIMESTAMPTZ DEFAULT NOW(),
            instances_received BIGINT NOT NULL DEFAULT 0,
            UNIQUE (ae_title)
        );
        """;

    public static async Task RunAsync(IServiceProvider services, ILogger logger)
    {
        try
        {
            var db = services.GetRequiredService<ArchiveDbContext>();
            await db.Database.ExecuteSqlRawAsync(Ddl);
            logger.LogInformation("Schema initializer: all tables verified/created");
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Schema initializer failed — database may be unavailable");
            throw; // Fail fast on startup so Aspire shows the error clearly
        }
    }
}
