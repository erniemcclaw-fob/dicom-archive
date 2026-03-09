"""
server.py — FastAPI server
Provides:
  - REST API for studies, series, instances (browse + query)
  - WADO-RS endpoints (retrieve pixel data for viewers like OHIF)
  - AE destinations CRUD
  - Routing rules CRUD
  - Manual route triggers
  - Background queue processor
  - Web management UI (served from /web)
"""

import os
import sys
import logging
import asyncio
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import pydicom

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agent'))
from storage import get_storage_backend

from db import get_db
from router import Router

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("dicom-server")

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="DICOM Archive", version="1.0.0", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

storage = get_storage_backend()
db      = get_db()
router  = Router(db, storage)

# ── Background queue processor ────────────────────────────────────────────────

async def run_queue_processor():
    """Process routing queue every 30 seconds."""
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, router.process_queue)
        except Exception as e:
            logger.error(f"Queue processor error: {e}")
        await asyncio.sleep(30)

@app.on_event("startup")
async def startup():
    asyncio.create_task(run_queue_processor())
    logger.info("DICOM Archive Server started")

# ── Helpers ───────────────────────────────────────────────────────────────────

def rows_to_list(rows) -> list:
    return [dict(r) for r in rows] if rows else []

def row_or_404(row, detail="Not found"):
    if not row:
        raise HTTPException(status_code=404, detail=detail)
    return dict(row)

def fetch_dicom_file(blob_key: str) -> pydicom.Dataset:
    """Retrieve a DICOM file from storage into memory."""
    import tempfile, shutil
    from storage import LocalStorage, S3Storage, AzureStorage

    tmp = tempfile.NamedTemporaryFile(suffix=".dcm", delete=False)
    tmp.close()
    try:
        if isinstance(storage, LocalStorage):
            src = storage.base / blob_key
            if not src.exists():
                raise HTTPException(404, f"Blob not found: {blob_key}")
            shutil.copy2(src, tmp.name)
        elif isinstance(storage, S3Storage):
            storage.s3.download_file(storage.bucket, blob_key, tmp.name)
        elif isinstance(storage, AzureStorage):
            blob = storage.client.get_blob_client(
                container=storage.container, blob=blob_key)
            with open(tmp.name, "wb") as f:
                storage.client.download_blob().readinto(f)
        return pydicom.dcmread(tmp.name)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  STUDIES API
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/studies")
def list_studies(
    patient_id: Optional[str] = None,
    modality:   Optional[str] = None,
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    limit:  int = Query(100, le=500),
    offset: int = 0,
):
    return rows_to_list(db.list_studies(patient_id, modality, date_from, date_to, limit, offset))


@app.get("/api/studies/{study_uid}")
def get_study(study_uid: str):
    return row_or_404(db.get_study(study_uid), "Study not found")


@app.get("/api/studies/{study_uid}/series")
def get_study_series(study_uid: str):
    return rows_to_list(db.get_series_for_study(study_uid))


@app.get("/api/series/{series_uid}/instances")
def get_series_instances(series_uid: str):
    return rows_to_list(db.get_instances_for_series(series_uid))


@app.get("/api/instances/{instance_uid}")
def get_instance(instance_uid: str):
    return row_or_404(db.get_instance(instance_uid), "Instance not found")


# ── Download raw DICOM file ───────────────────────────────────────────────────

@app.get("/api/instances/{instance_uid}/file")
def download_instance(instance_uid: str):
    inst = db.get_instance(instance_uid)
    if not inst:
        raise HTTPException(404, "Instance not found")
    import tempfile, shutil
    from storage import LocalStorage
    if isinstance(storage, LocalStorage):
        path = storage.base / inst["blob_key"]
        return FileResponse(
            str(path),
            media_type="application/dicom",
            filename=f"{instance_uid}.dcm"
        )
    # For cloud backends, stream through
    tmp = tempfile.NamedTemporaryFile(suffix=".dcm", delete=False)
    tmp.close()
    router._fetch_to_local(inst["blob_key"], tmp.name)
    return FileResponse(
        tmp.name,
        media_type="application/dicom",
        filename=f"{instance_uid}.dcm"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  WADO-RS  (minimal — enough for OHIF viewer)
# ═══════════════════════════════════════════════════════════════════════════════

WADO_BASE = "/wado/studies/{study_uid}"

@app.get("/wado/studies/{study_uid}/series/{series_uid}/instances/{instance_uid}")
def wado_retrieve_instance(study_uid: str, series_uid: str, instance_uid: str):
    """WADO-RS: retrieve a single instance as multipart/related."""
    inst = db.get_instance(instance_uid)
    if not inst:
        raise HTTPException(404, "Instance not found")

    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".dcm", delete=False)
    tmp.close()
    router._fetch_to_local(inst["blob_key"], tmp.name)
    raw = Path(tmp.name).read_bytes()
    Path(tmp.name).unlink(missing_ok=True)

    boundary = "DICOMwebBoundary"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/dicom\r\n\r\n"
    ).encode() + raw + f"\r\n--{boundary}--\r\n".encode()

    return Response(
        content=body,
        media_type=f"multipart/related; type=application/dicom; boundary={boundary}"
    )


@app.get("/wado/studies/{study_uid}/series/{series_uid}/instances/{instance_uid}/metadata")
def wado_metadata(study_uid: str, series_uid: str, instance_uid: str):
    """WADO-RS: JSON metadata for an instance."""
    inst = db.get_instance(instance_uid)
    if not inst:
        raise HTTPException(404)
    return JSONResponse(dict(inst))


# ═══════════════════════════════════════════════════════════════════════════════
#  AE DESTINATIONS
# ═══════════════════════════════════════════════════════════════════════════════

class DestinationIn(BaseModel):
    name:        str
    ae_title:    str
    host:        str
    port:        int = 104
    description: Optional[str] = None
    enabled:     bool = True

@app.get("/api/destinations")
def list_destinations():
    return rows_to_list(db.list_destinations())

@app.post("/api/destinations", status_code=201)
def create_destination(body: DestinationIn):
    try:
        return dict(db.create_destination(
            body.name, body.ae_title, body.host, body.port, body.description
        ))
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/destinations/{dest_id}")
def get_destination(dest_id: int):
    return row_or_404(db.get_destination(dest_id))

@app.put("/api/destinations/{dest_id}")
def update_destination(dest_id: int, body: DestinationIn):
    row = db.update_destination(dest_id,
        name=body.name, ae_title=body.ae_title.upper(),
        host=body.host, port=body.port,
        description=body.description, enabled=body.enabled
    )
    return row_or_404(row)

@app.delete("/api/destinations/{dest_id}", status_code=204)
def delete_destination(dest_id: int):
    db.delete_destination(dest_id)

@app.post("/api/destinations/{dest_id}/echo")
def echo_destination(dest_id: int):
    """Send a C-ECHO to verify connectivity to a destination AE."""
    dest = db.get_destination(dest_id)
    if not dest:
        raise HTTPException(404)
    from pynetdicom import AE as _AE
    from pynetdicom.sop_class import Verification
    ae = _AE("ARCHIVE_SCU")
    ae.add_requested_context(Verification)
    assoc = ae.associate(dest["host"], dest["port"], ae_title=dest["ae_title"])
    if not assoc.is_established:
        return {"ok": False, "message": "Association failed — check host/port/AE title"}
    status = assoc.send_c_echo()
    assoc.release()
    if status and status.Status == 0x0000:
        return {"ok": True, "message": "C-ECHO success"}
    return {"ok": False, "message": f"C-ECHO returned 0x{status.Status:04X}"}


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTING RULES
# ═══════════════════════════════════════════════════════════════════════════════

class RuleIn(BaseModel):
    name:             str
    destination_ids:  list[int]          # one or more — fan-out supported
    priority:         int  = 100
    enabled:          bool = True
    match_modality:   Optional[str] = None
    match_ae_title:   Optional[str] = None
    match_body_part:  Optional[str] = None
    on_receive:       bool = False        # opt-in — off by default
    description:      Optional[str] = None

@app.get("/api/rules")
def list_rules():
    return rows_to_list(db.list_rules())

@app.post("/api/rules", status_code=201)
def create_rule(body: RuleIn):
    if not body.destination_ids:
        raise HTTPException(400, "At least one destination is required")
    return dict(db.create_rule(
        body.name, body.destination_ids, body.priority,
        body.match_modality, body.match_ae_title,
        body.match_body_part, body.on_receive, body.description
    ))

@app.get("/api/rules/{rule_id}")
def get_rule(rule_id: int):
    return row_or_404(db.get_rule(rule_id))

@app.put("/api/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleIn):
    if not body.destination_ids:
        raise HTTPException(400, "At least one destination is required")
    row = db.update_rule(rule_id,
        destination_ids=body.destination_ids,
        name=body.name, priority=body.priority,
        enabled=body.enabled, match_modality=body.match_modality,
        match_ae_title=body.match_ae_title, match_body_part=body.match_body_part,
        on_receive=body.on_receive, description=body.description
    )
    return row_or_404(row)

@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int):
    db.delete_rule(rule_id)


# ═══════════════════════════════════════════════════════════════════════════════
#  INTERNAL — called by the ingest agent after each successful store
# ═══════════════════════════════════════════════════════════════════════════════

class IngestNotification(BaseModel):
    instance_id:  int
    instance_uid: str
    modality:     Optional[str] = None
    sending_ae:   Optional[str] = None
    body_part:    Optional[str] = None

@app.post("/internal/routed")
def on_instance_received(body: IngestNotification, background_tasks: BackgroundTasks):
    """
    Called by the ingest agent immediately after storing an instance.
    Evaluates on_receive routing rules and kicks off matching routes.
    If this endpoint is unreachable the queue processor retries within 30s.
    """
    queued = router.evaluate_and_queue(
        body.instance_id, body.modality or "",
        body.sending_ae or "", body.body_part or ""
    )
    if queued:
        background_tasks.add_task(router.process_queue)
    return {"ok": True, "routes_queued": queued}


# ═══════════════════════════════════════════════════════════════════════════════
#  MANUAL ROUTING
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/route/instance/{instance_uid}/to/{dest_id}")
def route_instance(instance_uid: str, dest_id: int, background_tasks: BackgroundTasks):
    """Manually route a single instance to a destination."""
    background_tasks.add_task(router.route_instance_to_destination, instance_uid, dest_id)
    return {"ok": True, "message": f"Routing {instance_uid} queued"}

@app.post("/api/route/study/{study_uid}/to/{dest_id}")
def route_study(study_uid: str, dest_id: int, background_tasks: BackgroundTasks):
    """Manually route all instances of a study to a destination."""
    background_tasks.add_task(router.route_study_to_destination, study_uid, dest_id)
    return {"ok": True, "message": f"Routing study {study_uid} queued"}


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTING LOG + STATS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/routing-log")
def routing_log(limit: int = Query(50, le=200)):
    return rows_to_list(db.list_routing_log(limit))

@app.get("/api/stats")
def stats():
    return dict(db.get_stats())


# ═══════════════════════════════════════════════════════════════════════════════
#  STATIC WEB UI
# ═══════════════════════════════════════════════════════════════════════════════

web_dir = Path(__file__).parent / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host=os.getenv("SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("SERVER_PORT", "8080")),
        reload=False,
    )
