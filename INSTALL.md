# DICOM Archive — Installation & Operations Guide

**Version:** 1.0  
**Repository:** https://github.com/erniemcclaw-fob/dicom-archive

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Prerequisites](#3-prerequisites)
4. [Installation](#4-installation)
5. [Configuration](#5-configuration)
6. [Starting the System](#6-starting-the-system)
7. [First-Time Setup in the Web UI](#7-first-time-setup-in-the-web-ui)
8. [Connecting a Modality](#8-connecting-a-modality)
9. [Configuring Routing Rules](#9-configuring-routing-rules)
10. [Verifying Everything Works](#10-verifying-everything-works)
11. [Cloud Storage (S3 / Azure)](#11-cloud-storage-s3--azure)
12. [Troubleshooting](#12-troubleshooting)
13. [Directory Structure](#13-directory-structure)
14. [Ports Reference](#14-ports-reference)
15. [Updating](#15-updating)

---

## 1. Overview

This system provides a lightweight, cloud-friendly DICOM archive. It accepts images
from any DICOM modality (mammography units, PACS, workstations), stores them as plain
files in local or cloud blob storage, and indexes the metadata in a Postgres database.

Images can then be:
- **Browsed** via the web management UI
- **Retrieved** or downloaded as DICOM files
- **Routed** automatically or manually to one or more destination AE titles

The system is intentionally DICOM-neutral after ingest — images are just files.
DICOM is only used at the network edges (receive from modality, send to destination).

---

## 2. Architecture

```
┌─────────────────────┐     DICOM C-STORE      ┌─────────────────────────────┐
│   Modality / PACS   │ ──────────────────────► │  Ingest Agent  (port 11112) │
│  (mammography unit, │                         │  • Accepts any SOP class    │
│   workstation, etc) │                         │  • SHA-256 checksums file   │
└─────────────────────┘                         │  • Stores to blob storage   │
                                                │  • Indexes in Postgres      │
                                                │  • Notifies routing server  │
                                                └──────────────┬──────────────┘
                                                               │
                                                    ┌──────────▼──────────┐
                                                    │      Postgres        │
                                                    │  patients / exams /  │
                                                    │  series / instances  │
                                                    │  destinations/rules  │
                                                    └──────────┬──────────┘
                                                               │
                                                ┌──────────────▼──────────────┐
┌─────────────────────┐     DICOM C-STORE      │  Server  (port 8080)        │
│  Destination AE     │ ◄────────────────────── │  • Web management UI        │
│  (PACS, viewer,     │                         │  • REST + WADO-RS API       │
│   workstation)      │                         │  • Routing engine           │
└─────────────────────┘                         │  • Queue processor          │
                                                └─────────────────────────────┘
                                                               ▲
                                                               │  Browser
                                                ┌─────────────┴─────────────┐
                                                │     Your Web Browser       │
                                                │  http://<host>:8080        │
                                                └───────────────────────────┘
```

**Three containers run via Docker Compose:**

| Container | Purpose | Port |
|-----------|---------|------|
| `agent` | DICOM SCP — receives images from modalities | 11112 |
| `server` | Web UI, REST API, routing engine | 8080 |
| `postgres` | Metadata database | 5432 (internal) |

---

## 3. Prerequisites

### Required on the host machine

| Software | Version | Notes |
|----------|---------|-------|
| Docker | 24+ | https://docs.docker.com/get-docker/ |
| Docker Compose | v2 (built into Docker) | Run `docker compose version` to check |
| Git | Any | To clone the repository |

> **Windows users:** Use Docker Desktop for Windows with WSL2 backend.  
> **macOS users:** Docker Desktop for Mac works as-is.  
> **Linux users:** Install Docker Engine + the Compose plugin.

### Network requirements

- Port **11112** must be reachable from your modality/PACS network
- Port **8080** must be reachable from browsers that will use the web UI
- The archive host needs outbound internet access only if using cloud storage (S3/Azure)

### Minimum hardware

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 2 cores | 4 cores |
| RAM | 4 GB | 8 GB |
| Disk | 50 GB | Size to your expected image volume |

> Mammography images are typically 20–80 MB each uncompressed.
> Plan storage accordingly — 1,000 exams at 4 images each ≈ 320 GB.

---

## 4. Installation

### Step 1 — Clone the repository

```bash
git clone https://github.com/erniemcclaw-fob/dicom-archive.git
cd dicom-archive
```

### Step 2 — Create the agent configuration file

```bash
cp agent/.env.example agent/.env
```

Open `agent/.env` in a text editor. The defaults work for a basic local installation —
you only **must** change things if you're using cloud storage. See [Section 5](#5-configuration).

### Step 3 — Create the data directories

```bash
mkdir -p data/received data/quarantine
```

These are mounted into the containers as volumes:

| Directory | Purpose |
|-----------|---------|
| `data/received/` | Stored DICOM files (the archive itself) |
| `data/quarantine/` | Files that failed validation (for manual review) |

### Step 4 — (Optional) Change the database password

The default password in `docker-compose.yml` is `changeme`. For a lab or production
environment, replace it in two places:

In `docker-compose.yml`, find the `postgres` service:
```yaml
environment:
  POSTGRES_PASSWORD: changeme        # ← change this
```

And in both `agent` and `server` environment sections:
```yaml
DATABASE_URL: postgresql://dicom:changeme@postgres:5432/dicom_archive
                                   # ↑ match the password above
```

---

## 5. Configuration

All agent configuration lives in `agent/.env`.

### Storage backend

The system supports three storage backends. Set `STORAGE_BACKEND` to one of:

#### Local filesystem (default — good for lab use)

```env
STORAGE_BACKEND=local
LOCAL_STORAGE_PATH=./received
```

Files are stored on the host under `data/received/` in a hierarchy:
```
data/received/
  20260309/                          ← study date
    1.2.840.10008.5.1.4.1.1.1.2/    ← study UID
      1.2.840.10008.5.1.4.1.1.1.2.1/ ← series UID
        1.2.3.4.5.dcm                 ← instance UID
```

#### AWS S3

```env
STORAGE_BACKEND=s3
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
AWS_REGION=us-east-1
S3_BUCKET=your-dicom-archive-bucket
```

Create the S3 bucket before starting. Enable versioning if you want extra protection.

#### Azure Blob Storage

```env
STORAGE_BACKEND=azure
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...
AZURE_CONTAINER=dicom-archive
```

The container will be created automatically if it doesn't exist.

---

### DICOM agent settings

```env
# The AE title this archive presents to the network.
# Modalities will need to know this when they configure a DICOM destination.
AE_TITLE=ARCHIVE_SCP

# Port to listen for incoming DICOM associations.
# Standard DICOM port is 104, but ports above 1024 don't require root on Linux.
LISTEN_PORT=11112
LISTEN_HOST=0.0.0.0

# Auto-routing: URL of the server container.
# Leave as-is for Docker Compose — containers communicate by service name.
ROUTER_URL=http://server:8080
```

---

## 6. Starting the System

### Start all services

```bash
docker compose up -d
```

Docker will:
1. Build the agent and server images (first run takes 2–3 minutes)
2. Start Postgres and wait for it to be healthy
3. Start the agent and server

### Check that everything is running

```bash
docker compose ps
```

Expected output:
```
NAME                    STATUS          PORTS
dicom-archive-agent-1   Up              0.0.0.0:11112->11112/tcp
dicom-archive-server-1  Up              0.0.0.0:8080->8080/tcp
dicom-archive-postgres-1 Up (healthy)   0.0.0.0:5432->5432/tcp
```

### View logs

```bash
# All services
docker compose logs -f

# Just the agent (shows each received image)
docker compose logs -f agent

# Just the server
docker compose logs -f server
```

### Stop the system

```bash
docker compose down
```

> Data is preserved in the `data/` directory and the `pgdata` Docker volume.
> Use `docker compose down -v` only if you want to wipe the database.

---

## 7. First-Time Setup in the Web UI

Open a browser and go to: **http://\<your-host-ip\>:8080**

You should see the DICOM Archive dashboard. On first launch everything will show zeros —
that's normal. Follow these steps to configure the system.

---

### Step 7a — Add a Destination AE

A **destination** is a remote DICOM system you want to forward images to
(e.g., a PACS, a workstation, another archive, a viewing system).

1. Click **Destinations** in the left sidebar
2. Click **+ Add Destination**
3. Fill in the form:

   | Field | Description | Example |
   |-------|-------------|---------|
   | Friendly Name | A human-readable label | Main PACS |
   | AE Title | The remote system's DICOM AE title | PACS_SCP |
   | Host / IP | IP address or hostname | 192.168.1.50 |
   | Port | DICOM port on the remote system | 104 |
   | Description | Optional notes | Main departmental PACS |
   | Enabled | Toggle on to allow routing to this destination | ✓ |

4. Click **Save**
5. Click **🔔 Echo** next to the new destination to send a C-ECHO and verify connectivity

> If the echo fails, check:
> - The IP address and port are correct
> - The remote system has the archive's AE title in its allowed callers list
> - No firewall is blocking the connection

---

### Step 7b — Add a Routing Rule

A **routing rule** defines which images should go where, and whether routing happens
automatically or must be triggered manually.

1. Click **Rules** in the left sidebar
2. Click **+ Add Rule**
3. Fill in the form:

   **Destinations** — tick one or more checkboxes. Images matching this rule will be
   sent to *all* selected destinations (fan-out).

   **Match Criteria** — all fields are optional. Leaving a field blank means "match
   everything." You can match on:

   | Field | Example | Notes |
   |-------|---------|-------|
   | Modality | `MG` | Mammography. Other examples: `CT`, `MR`, `CR`, `DX` |
   | Sending AE Title | `MAMMO_UNIT` | The AE title of the modality sending the image |
   | Body Part | `BREAST` | As tagged in the DICOM header |

   **Auto-route on receipt** — when this toggle is **on**, matching images are forwarded
   to the selected destinations automatically the moment they are received, with no
   manual action required. When **off**, the rule only applies to manual routing from
   the Studies page.

   **Priority** — if multiple rules match the same image, lower numbers run first.
   Use this to create specific rules (priority 10) that override general ones (priority 100).

4. Click **Save Rule**

---

### Example rule configurations

**Forward all mammography automatically to PACS:**
- Name: `All MG → Main PACS`
- Destinations: ✓ Main PACS
- Modality: `MG`
- Auto-route on receipt: **ON**
- Priority: `10`

**Mirror everything to a backup archive:**
- Name: `All images → Backup`
- Destinations: ✓ Backup Archive
- (leave all match fields blank — matches everything)
- Auto-route on receipt: **ON**
- Priority: `100`

**Forward to two destinations simultaneously:**
- Name: `MG → PACS + Backup`
- Destinations: ✓ Main PACS  ✓ Backup Archive
- Modality: `MG`
- Auto-route on receipt: **ON**

---

## 8. Connecting a Modality

On your mammography unit or PACS, add a new DICOM destination (sometimes called
a "DICOM node", "AE", or "store destination") with these settings:

| Setting | Value |
|---------|-------|
| AE Title | `ARCHIVE_SCP` (or whatever you set in `agent/.env`) |
| Host / IP | IP address of the machine running Docker |
| Port | `11112` |

> **Finding your host IP:**
> - Linux/macOS: run `ip addr` or `ifconfig`
> - Windows: run `ipconfig`
> Use the LAN IP (typically 192.168.x.x or 10.x.x.x), not localhost.

### Test from the modality

Most modalities have a built-in DICOM echo/ping button in their network configuration.
Use it to confirm the modality can reach the archive before sending real images.

### Test with a DICOM toolkit (optional)

If you have `dcmtk` installed on any machine on the network:

```bash
# Send a C-ECHO to verify the agent is listening
echoscu -aec ARCHIVE_SCP <archive-host> 11112

# Send a test DICOM file
storescu -aec ARCHIVE_SCP <archive-host> 11112 /path/to/test.dcm
```

---

## 9. Configuring Routing Rules

### How routing works

1. An image arrives via C-STORE
2. The agent stores it and notifies the server
3. The server checks all enabled rules with **Auto-route on receipt = ON**
4. For each matching rule, one routing log entry is created per destination
5. The routing engine sends the image via C-STORE to each destination
6. Results appear in the **Route Log**

If the server is unavailable when an image arrives, the agent logs a warning and
continues. The server's background queue processor checks for pending routes every
30 seconds, so nothing is lost.

### Manual routing

From the **Studies** page:
1. Find the study you want to route
2. In the **Route to…** dropdown at the end of the row, select a destination
3. Click **▶**

To route individual instances, drill down into a series and use the per-instance
**Route…** dropdown.

### Monitoring routing

Click **Route Log** in the sidebar to see:
- Every routing attempt with timestamp
- Status: `queued` → `sending` → `success` / `failed`
- Number of attempts (retried up to 3 times automatically)
- Error message on failure

---

## 10. Verifying Everything Works

### End-to-end test checklist

- [ ] `docker compose ps` shows all three containers as **Up**
- [ ] Web UI loads at `http://<host>:8080`
- [ ] C-ECHO from the web UI to a destination returns success
- [ ] Send a test DICOM file from a modality or `storescu`
- [ ] Agent log shows `✓ Stored <blob-key>`
- [ ] Study appears in the **Studies** page of the web UI
- [ ] If auto-routing is configured: Route Log shows a `success` entry

### Check the agent received an image

```bash
docker compose logs agent | grep "✓ Stored"
```

Expected:
```
[INFO] dicom-agent — ✓ Stored 20260309/1.2.3.../1.2.3.4.dcm
```

### Check the database directly (optional)

```bash
docker compose exec postgres psql -U dicom -d dicom_archive -c \
  "SELECT p.patient_id, e.study_date, e.modality, COUNT(i.id) AS images
   FROM patients p
   JOIN exams e ON e.patient_id = p.id
   JOIN series s ON s.exam_id = e.id
   JOIN instances i ON i.series_id = s.id
   GROUP BY p.patient_id, e.study_date, e.modality
   ORDER BY e.study_date DESC LIMIT 10;"
```

---

## 11. Cloud Storage (S3 / Azure)

### AWS S3 setup

1. Create an S3 bucket in the AWS Console (or CLI)
2. Create an IAM user with `s3:PutObject`, `s3:GetObject`, `s3:HeadObject` on the bucket
3. Generate an access key for that user
4. Edit `agent/.env`:

```env
STORAGE_BACKEND=s3
AWS_ACCESS_KEY_ID=<your-key-id>
AWS_SECRET_ACCESS_KEY=<your-secret>
AWS_REGION=us-east-1
S3_BUCKET=your-bucket-name
```

5. Set the same variables in `docker-compose.yml` under the `server` service
   (the server needs to read blobs back for retrieval and routing)

### Azure Blob Storage setup

1. Create a Storage Account in the Azure Portal
2. Note the connection string from **Access keys** (under Security + Networking)
3. Edit `agent/.env`:

```env
STORAGE_BACKEND=azure
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...
AZURE_CONTAINER=dicom-archive
```

4. Set the same variables in `docker-compose.yml` under the `server` service

> **Tip:** For Azure, consider setting a lifecycle policy on the container to move
> blobs to Cool or Archive tier after 90 days to reduce storage costs.

---

## 12. Troubleshooting

### Agent won't start

**Symptom:** `docker compose ps` shows agent as `Exited`

```bash
docker compose logs agent
```

Common causes:
- Port 11112 already in use on the host → change `LISTEN_PORT` in `agent/.env`
  and update the port mapping in `docker-compose.yml`
- Postgres not yet healthy → wait 10–15 seconds and retry

---

### Modality can't connect

**Symptom:** Modality reports "connection refused" or "association rejected"

Checklist:
1. Confirm the archive IP and port are correct on the modality
2. Run `docker compose ps` — is the agent running?
3. Check firewall: `telnet <archive-host> 11112` should connect
4. Check the AE title configured on the modality matches `AE_TITLE` in `agent/.env`
5. Check agent logs: `docker compose logs agent`

---

### Images received but not appearing in the web UI

**Symptom:** Agent log shows `✓ Stored` but Studies page is empty

```bash
docker compose logs server
```

- If server shows a DB connection error, Postgres may have restarted → `docker compose restart server`
- Check `DATABASE_URL` is set correctly in `docker-compose.yml`

---

### Routing fails

**Symptom:** Route Log shows `failed` status

1. Click the error message in the Route Log to see the full error
2. Common causes:
   - Destination host/port/AE title is wrong → edit the destination and re-test echo
   - Destination system is offline → retry will happen automatically (up to 3 attempts)
   - Destination AE title doesn't accept our calling AE title → check destination's allowed callers list

---

### Files in quarantine

Images land in `data/quarantine/` when they fail validation. Common reasons:
- Missing required DICOM tags (no SOPInstanceUID, StudyInstanceUID, or SeriesInstanceUID)
- No pixel data in the file
- File exceeded `MAX_FILE_BYTES` limit

To inspect a quarantined file:
```bash
# List quarantined files
ls data/quarantine/

# Examine DICOM tags (requires dcmtk)
dcmdump data/quarantine/some-file.dcm | head -50
```

---

### Reset everything and start fresh

> ⚠️ This deletes all stored images and database records.

```bash
docker compose down -v          # stop containers and delete DB volume
rm -rf data/received/*          # delete stored files
rm -rf data/quarantine/*        # delete quarantined files
docker compose up -d            # restart fresh
```

---

## 13. Directory Structure

```
dicom-archive/
├── agent/                      Ingest agent source
│   ├── agent.py                Main DICOM SCP service
│   ├── storage.py              Pluggable blob storage (local/S3/Azure)
│   ├── database.py             Postgres schema + write queries
│   ├── .env                    Your local configuration (not committed to git)
│   ├── .env.example            Configuration template
│   ├── Dockerfile
│   └── requirements.txt
│
├── server/                     API server + web UI source
│   ├── server.py               FastAPI application
│   ├── db.py                   Postgres read/write queries
│   ├── router.py               Routing engine (C-STORE SCU)
│   ├── web/
│   │   └── index.html          Web management UI
│   ├── Dockerfile
│   └── requirements.txt
│
├── data/                       Runtime data (created by you, not in git)
│   ├── received/               Stored DICOM files
│   └── quarantine/             Failed/rejected files for review
│
├── docker-compose.yml          Orchestration
└── INSTALL.md                  This document
```

---

## 14. Ports Reference

| Port | Protocol | Service | Direction | Notes |
|------|----------|---------|-----------|-------|
| 11112 | TCP | DICOM SCP (agent) | Inbound from modalities | Must be reachable from modality network |
| 8080 | TCP | Web UI + REST API (server) | Inbound from browsers | Restrict to internal network |
| 5432 | TCP | Postgres | Internal only | Not exposed externally by default |

> **Security note:** Port 8080 has no authentication in the current version.
> Restrict access using a firewall rule or reverse proxy (nginx with basic auth)
> if deploying outside a trusted lab network.

---

## 15. Updating

To pull the latest code and rebuild:

```bash
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

The database schema is updated automatically on startup. Your data is preserved.

---

## Quick Reference Card

```
Start:    docker compose up -d
Stop:     docker compose down
Logs:     docker compose logs -f agent
          docker compose logs -f server
Status:   docker compose ps
Web UI:   http://<host>:8080
DICOM:    <host>:11112  AE: ARCHIVE_SCP

Test echo (dcmtk):
  echoscu -aec ARCHIVE_SCP <host> 11112

Send test file (dcmtk):
  storescu -aec ARCHIVE_SCP <host> 11112 test.dcm

DB shell:
  docker compose exec postgres psql -U dicom -d dicom_archive
```

---

*For questions or issues, refer to the project repository:*  
*https://github.com/erniemcclaw-fob/dicom-archive*
