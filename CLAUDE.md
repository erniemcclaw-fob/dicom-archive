# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DICOM Archive is a lightweight, cloud-friendly medical imaging archival system. It accepts DICOM images from modalities via C-STORE, stores them to local or cloud blob storage, indexes metadata in PostgreSQL, and provides a REST API, routing engine, and web management UI.

## Architecture

```
Modality/PACS ──C-STORE──▶ Ingest Agent (Python, :11112)
                                │
                                ▼
                           PostgreSQL ◀── Server (.NET 10, :8080)
                                              │
                                              ▼
                                         Browser / OHIF Viewer
```

- **agent/** — Python DICOM C-STORE SCP. Receives images, validates pixel data, computes SHA-256, stores to blob backend, indexes in Postgres, registers with server, sends 60s heartbeats, notifies routing engine.
- **server-dotnet/** — .NET 10 minimal API server (primary). REST API, WADO-RS, web UI, routing engine with background queue processor (30s interval), C-STORE SCU for forwarding.
- **server/** — Legacy Python FastAPI server (maintained for reference).
- **aspire/DicomArchive.AppHost/** — .NET Aspire orchestration (Postgres, server, agent).
- **aspire/DicomArchive.ServiceDefaults/** — Shared Aspire config (OpenTelemetry, health checks, service discovery).

## Build & Run

### Docker Compose (recommended)
```bash
cp agent/.env.example agent/.env
mkdir -p data/received data/quarantine
docker compose up -d
docker compose logs -f
```

### .NET Aspire
```bash
# Requires .NET 10 SDK + Aspire workload
dotnet run --project aspire/DicomArchive.AppHost
```

### .NET Server only
```bash
dotnet build DicomArchive.slnx
dotnet run --project server-dotnet/DicomArchive.Server
```

### Rebuild after changes
```bash
docker compose down && docker compose build --no-cache && docker compose up -d
```

## Database

PostgreSQL with idempotent DDL (`CREATE TABLE IF NOT EXISTS`) — no EF Core migrations. Schema is initialized on startup by `SchemaInitializer.cs` (.NET) and `database.py` (Python agent).

**Core tables:** patients, exams, series, instances (DICOM hierarchy).
**Routing tables:** ae_destinations, routing_rules, rule_destinations (join), routing_log.
**Agent registry:** agents (tracks online/offline status via heartbeats).

## Key .NET Server Structure (server-dotnet/DicomArchive.Server/)

- `Program.cs` — DI setup, schema init, endpoint mapping
- `Data/SchemaInitializer.cs` — DDL initialization
- `Services/RouterService.cs` — Rule evaluation, C-STORE SCU sending, OpenTelemetry tracing
- `Services/StorageService.cs` — Blob retrieval (local/S3/Azure)
- `Services/QueueProcessorService.cs` — Background routing queue (IServiceScopeFactory for DI scoping)
- `Endpoints/` — Organized by domain: StudyEndpoints, DestinationEndpoints, RuleEndpoints, AgentEndpoints, InternalEndpoints, WadoEndpoints
- `wwwroot/index.html` — Pre-compiled SPA for management UI

## Key Dependencies

- **fo-dicom** (5.2.2) — DICOM protocol for .NET
- **pynetdicom** / **pydicom** — DICOM protocol for Python agent
- **Aspire.Npgsql.EntityFrameworkCore.PostgreSQL** — DB integration via Aspire
- DbContext registered both as scoped and via `IDbContextFactory` (needed by background services)

## Configuration

Agent configuration via `agent/.env` (see `agent/.env.example`). Key vars:
- `AE_TITLE`, `LISTEN_PORT`, `STORAGE_BACKEND` (local/s3/azure), `DATABASE_URL`, `ROUTER_URL`

.NET server configured via Aspire environment injection and `appsettings.json`.

## Storage Backends

Files organized as `YYYYMMDD/{study_uid}/{series_uid}/{instance_uid}.dcm`. Three backends:
- **local** — Filesystem under `LOCAL_STORAGE_PATH`
- **s3** — AWS S3 (boto3 in agent; stub in .NET)
- **azure** — Azure Blob Storage (agent; stub in .NET)

## Multi-Site Support

Multiple agents can share one server/database. Each agent has a unique `AE_TITLE`. Routing rules can target specific agents via `match_receiving_ae`. Server tracks agent status (3-minute offline threshold).

## Testing DICOM Ingest

```bash
# Send test file with DCMTK
storescu -aec ARCHIVE_SCP <host> 11112 /path/to/test.dcm

# Verify
curl http://localhost:8080/api/studies
```

## Operations Guide

See `INSTALL.md` for comprehensive installation, configuration, and operations documentation.
