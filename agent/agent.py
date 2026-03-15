"""
agent.py — DICOM Archive Edge Agent

Accepts DICOM C-STORE associations from any modality (optimised for mammography).
For each received instance:
  1. Validates it is a real DICOM file with pixel data
  2. Checksums it (SHA-256) — lossless verification
  3. Saves to local staging directory
  4. Enqueues for 3-step upload handshake with the cloud server:
     ① POST /ingest/prepare   → get upload URL + instance_id
     ② PUT  .dcm to blob      → direct upload via pre-signed URL
     ③ POST /ingest/confirm   → mark stored, trigger routing
     ④ Delete local staging file

The agent has NO direct database access and NO cloud storage credentials.
All metadata writes go through the server API. File uploads use time-limited
pre-signed URLs / SAS tokens for maximum throughput with parallel workers.
"""

import os
import sys
import logging
import tempfile
import shutil
import asyncio
import threading
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import urllib.request
import urllib.error
import json as _json

import pydicom
from pynetdicom import AE, evt, AllStoragePresentationContexts, ALL_TRANSFER_SYNTAXES
from pynetdicom.sop_class import Verification

from uploader import UploadEngine, sha256_of_file

# ── Logging ───────────────────────────────────────────────────────────────────

SEQ_URL = os.getenv("SEQ_URL", "").rstrip("/")

if SEQ_URL:
    from seqlog import log_to_seq
    log_to_seq(
        server_url=SEQ_URL,
        level=logging.INFO,
        batch_size=10,
        auto_flush_timeout=2,
        override_root_logger=True,
        additional_handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger("dicom-agent")
    logger.info("Seq logging enabled at %s", SEQ_URL)
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("agent.log"),
        ]
    )
    logger = logging.getLogger("dicom-agent")

AGENT_VERSION = "2.0.0"

# ── Config ────────────────────────────────────────────────────────────────────

AE_TITLE       = os.getenv("AE_TITLE",     "ARCHIVE_SCP")
LISTEN_PORT    = int(os.getenv("LISTEN_PORT", "11112"))
LISTEN_HOST    = os.getenv("LISTEN_HOST",  "0.0.0.0")
QUARANTINE     = Path(os.getenv("QUARANTINE_PATH", "./quarantine"))
STAGING_DIR    = Path(os.getenv("STAGING_PATH", "./staging"))
MAX_BYTES      = int(os.getenv("MAX_FILE_BYTES", "0"))  # 0 = unlimited
SERVER_URL     = os.getenv("SERVER_URL", os.getenv("ROUTER_URL", "")).rstrip("/")
AGENT_API_KEY  = os.getenv("AGENT_API_KEY", "")
UPLOAD_WORKERS = int(os.getenv("UPLOAD_WORKERS", "4"))

QUARANTINE.mkdir(parents=True, exist_ok=True)
STAGING_DIR.mkdir(parents=True, exist_ok=True)

# ── Upload engine (initialized in run()) ─────────────────────────────────────

upload_engine: UploadEngine | None = None
_event_loop: asyncio.AbstractEventLoop | None = None

# ── Registration + Heartbeat ──────────────────────────────────────────────────

def _get_host() -> str:
    import socket
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"

def _server_post(path: str, payload: dict, label: str = ""):
    """POST JSON to the server with API key auth."""
    if not SERVER_URL:
        return None
    data = _json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            f"{SERVER_URL}{path}",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": AGENT_API_KEY,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        logger.warning(f"  Server {label or path} failed: {e}")
        return None

def register_with_server():
    result = _server_post("/ingest/register", {
        "ae_title":        AE_TITLE,
        "host":            _get_host(),
        "storage_backend": "edge",
        "version":         AGENT_VERSION,
    }, label="registration")
    if result and result.get("ok"):
        logger.info(f"Registered with server as [{AE_TITLE}]")
    else:
        logger.warning("Could not register with server — will retry on next heartbeat")

def _heartbeat_loop(interval: int = 60):
    import threading
    _counter = {"since_last": 0}

    def beat():
        while True:
            import time
            time.sleep(interval)
            delta = _counter["since_last"]
            _counter["since_last"] = 0
            result = _server_post("/ingest/heartbeat", {
                "ae_title":        AE_TITLE,
                "instances_delta": delta,
            }, label="heartbeat")
            if result is None:
                register_with_server()

    t = threading.Thread(target=beat, daemon=True, name="heartbeat")
    t.start()
    return _counter

_instance_counter = {"since_last": 0}


# ── DICOM validation ──────────────────────────────────────────────────────────

def validate(ds, path: str) -> tuple[bool, str]:
    for tag in ("SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID"):
        if not hasattr(ds, tag):
            return False, f"Missing {tag}"

    if not hasattr(ds, "PixelData"):
        return False, "No PixelData"

    if MAX_BYTES > 0:
        size = Path(path).stat().st_size
        if size > MAX_BYTES:
            return False, f"File {size} bytes exceeds limit {MAX_BYTES}"

    modality = str(getattr(ds, "Modality", ""))
    if modality == "MG":
        if not getattr(ds, "Laterality", None):
            logger.warning("Mammo image missing Laterality tag")
        if not getattr(ds, "ViewPosition", None):
            logger.warning("Mammo image missing ViewPosition tag")

    return True, "ok"


# ── Core handler ──────────────────────────────────────────────────────────────

def handle_store(event):
    """
    Called for every C-STORE request received.
    Validates, checksums, saves to local staging, and enqueues for async upload.
    """
    ds          = event.dataset
    ds.file_meta = event.file_meta
    sending_ae  = event.assoc.requestor.ae_title.strip()

    instance_uid = str(ds.SOPInstanceUID)
    logger.info(f"Receiving  {instance_uid}  from  [{sending_ae}]")

    # Write to a temp file first (we want the raw bytes exactly as received)
    tmp_dir  = tempfile.mkdtemp(prefix="dcm_ingest_")
    tmp_path = os.path.join(tmp_dir, f"{instance_uid}.dcm")

    try:
        pydicom.dcmwrite(tmp_path, ds, write_like_original=True)

        # ── Validate ──
        ok, reason = validate(ds, tmp_path)
        if not ok:
            logger.error(f"Validation FAILED for {instance_uid}: {reason}")
            _quarantine(tmp_path, instance_uid, reason)
            return 0xA700

        # ── Checksum ──
        checksum   = sha256_of_file(tmp_path)
        size_bytes = Path(tmp_path).stat().st_size
        logger.info(f"  SHA-256: {checksum}  size: {size_bytes:,} bytes")

        # ── Move to staging directory ──
        staging_path = str(STAGING_DIR / f"{instance_uid}.dcm")
        shutil.move(tmp_path, staging_path)

        # ── Build metadata for upload handshake ──
        study_date = str(getattr(ds, "StudyDate", "UNKNOWN"))
        metadata = {
            "ae_title":        AE_TITLE,
            "sending_ae":      sending_ae,
            "patient_id":      str(getattr(ds, "PatientID", "UNKNOWN")),
            "patient_name":    str(getattr(ds, "PatientName", "")) or None,
            "study_uid":       str(ds.StudyInstanceUID),
            "study_date":      study_date,
            "series_uid":      str(ds.SeriesInstanceUID),
            "instance_uid":    instance_uid,
            "modality":        str(getattr(ds, "Modality", "") or ""),
            "body_part":       str(getattr(ds, "BodyPartExamined", "") or ""),
            "file_size_bytes": size_bytes,
            "sha256":          checksum,
        }

        # ── Enqueue for async upload ──
        if upload_engine and _event_loop:
            asyncio.run_coroutine_threadsafe(
                upload_engine.enqueue(staging_path, metadata),
                _event_loop,
            )
        else:
            logger.warning("Upload engine not running — file staged at %s", staging_path)

        _instance_counter["since_last"] += 1
        logger.info(f"  ✓ Staged  {instance_uid}")
        return 0x0000  # C-STORE success

    except Exception as e:
        logger.exception(f"Unexpected error processing {instance_uid}: {e}")
        _quarantine(tmp_path, instance_uid, str(e))
        return 0xA700

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _quarantine(src_path: str, uid: str, reason: str):
    dest = QUARANTINE / f"{uid}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.dcm"
    try:
        shutil.move(src_path, dest)
        logger.warning(f"  → Quarantined {dest}  reason: {reason}")
    except Exception as e:
        logger.error(f"  Could not quarantine {src_path}: {e}")


# ── C-ECHO handler ────────────────────────────────────────────────────────────

def handle_echo(event):
    logger.info(f"C-ECHO from [{event.assoc.requestor.ae_title.strip()}]")
    return 0x0000


# ── Async event loop (runs in background thread) ─────────────────────────────

def _start_async_loop(loop: asyncio.AbstractEventLoop, engine: UploadEngine):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(engine.start())
    loop.run_forever()


# ── Build and start the AE ────────────────────────────────────────────────────

def run():
    global upload_engine, _event_loop, _instance_counter

    # ── Start upload engine in a background asyncio loop ──
    if SERVER_URL and AGENT_API_KEY:
        _event_loop = asyncio.new_event_loop()
        upload_engine = UploadEngine(
            server_url=SERVER_URL,
            api_key=AGENT_API_KEY,
            workers=UPLOAD_WORKERS,
        )
        loop_thread = threading.Thread(
            target=_start_async_loop,
            args=(_event_loop, upload_engine),
            daemon=True,
            name="upload-loop",
        )
        loop_thread.start()
        logger.info("Upload engine thread started (%d workers)", UPLOAD_WORKERS)
    else:
        if not SERVER_URL:
            logger.warning("SERVER_URL not set — uploads disabled")
        if not AGENT_API_KEY:
            logger.warning("AGENT_API_KEY not set — uploads disabled")

    ae = AE(ae_title=AE_TITLE)

    # Accept C-ECHO (Verification)
    ae.add_supported_context(Verification)

    # Accept ALL storage SOP classes with ALL transfer syntaxes
    for cx in AllStoragePresentationContexts:
        ae.add_supported_context(cx.abstract_syntax, ALL_TRANSFER_SYNTAXES)

    handlers = [
        (evt.EVT_C_STORE, handle_store),
        (evt.EVT_C_ECHO,  handle_echo),
    ]

    logger.info(f"╔══════════════════════════════════════════╗")
    logger.info(f"║  DICOM Archive Edge Agent v{AGENT_VERSION}        ║")
    logger.info(f"║  AE Title : {AE_TITLE:<30}║")
    logger.info(f"║  Listening: {LISTEN_HOST}:{LISTEN_PORT:<26}║")
    logger.info(f"║  Server   : {SERVER_URL or 'not configured':<30}║")
    logger.info(f"║  Workers  : {UPLOAD_WORKERS:<30}║")
    logger.info(f"║  Staging  : {str(STAGING_DIR):<30}║")
    logger.info(f"╚══════════════════════════════════════════╝")

    # Register with server and start heartbeat thread
    if SERVER_URL and AGENT_API_KEY:
        register_with_server()
        _instance_counter = _heartbeat_loop(interval=60)

    ae.start_server(
        (LISTEN_HOST, LISTEN_PORT),
        evt_handlers=handlers,
        block=True,
    )


if __name__ == "__main__":
    run()
