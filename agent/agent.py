"""
agent.py — DICOM Archive Ingest Agent

Accepts DICOM C-STORE associations from any modality (optimised for mammography).
For each received instance:
  1. Validates it is a real DICOM file with pixel data
  2. Checksums it (SHA-256) — lossless verification
  3. Stores the raw file to the configured blob backend (local / S3 / Azure)
  4. Indexes key metadata in Postgres (optional)
  5. Acknowledges success back to the sender

The file is never modified — the original DICOM bytes are what gets archived.
DICOM is only used at the network edge; after receipt it's just a file.
"""

import os
import sys
import logging
import tempfile
import shutil
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

from storage import get_storage_backend, sha256_of_file
from database import get_database

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("agent.log"),
    ]
)
logger = logging.getLogger("dicom-agent")

AGENT_VERSION = "1.0.0"

# ── Config ────────────────────────────────────────────────────────────────────

AE_TITLE      = os.getenv("AE_TITLE",     "ARCHIVE_SCP")
LISTEN_PORT   = int(os.getenv("LISTEN_PORT", "11112"))
LISTEN_HOST   = os.getenv("LISTEN_HOST",  "0.0.0.0")
QUARANTINE    = Path(os.getenv("QUARANTINE_PATH", "./quarantine"))
MAX_BYTES     = int(os.getenv("MAX_FILE_BYTES", "0"))  # 0 = unlimited
# Optional: URL of the server's internal notification endpoint.
# When set, the agent pings it after each successful store so on_receive
# routing rules fire immediately rather than waiting for the 30s queue poll.
ROUTER_URL    = os.getenv("ROUTER_URL", "").rstrip("/")  # e.g. http://server:8080

QUARANTINE.mkdir(parents=True, exist_ok=True)

# ── Registration + Heartbeat ──────────────────────────────────────────────────

def _get_host() -> str:
    """Best-effort: return this machine's hostname or IP."""
    import socket
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"

def _server_post(path: str, payload: dict, label: str = ""):
    """POST JSON to the server. Returns parsed response or None on failure."""
    if not ROUTER_URL:
        return None
    data = _json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            f"{ROUTER_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        logger.warning(f"  Server {label or path} failed: {e}")
        return None

def register_with_server():
    """Register this agent with the server on startup."""
    result = _server_post("/internal/register", {
        "ae_title":        AE_TITLE,
        "host":            _get_host(),
        "storage_backend": os.getenv("STORAGE_BACKEND", "local"),
        "version":         AGENT_VERSION,
    }, label="registration")
    if result and result.get("ok"):
        logger.info(f"Registered with server as [{AE_TITLE}]")
    else:
        logger.warning("Could not register with server — will retry on next heartbeat")

def _heartbeat_loop(interval: int = 60):
    """
    Background thread: sends a heartbeat to the server every `interval` seconds.
    Tracks instances stored since the last heartbeat for the server's counter.
    """
    import threading
    _counter = {"since_last": 0}

    def beat():
        while True:
            import time
            time.sleep(interval)
            delta = _counter["since_last"]
            _counter["since_last"] = 0
            result = _server_post("/internal/heartbeat", {
                "ae_title":        AE_TITLE,
                "instances_delta": delta,
            }, label="heartbeat")
            if result is None:
                # Server unreachable — re-register on next successful heartbeat
                register_with_server()

    t = threading.Thread(target=beat, daemon=True, name="heartbeat")
    t.start()
    return _counter  # caller can increment _counter["since_last"]

# Shared instance counter incremented by handle_store
_instance_counter = {"since_last": 0}

# ── Storage + DB (initialised once at startup) ────────────────────────────────

storage = get_storage_backend()
db      = get_database()

# ── Blob key builder ──────────────────────────────────────────────────────────

def make_blob_key(ds) -> str:
    """
    Hierarchical key:  StudyDate / StudyUID / SeriesUID / SOPInstanceUID.dcm
    Keeps things organised without requiring any DICOM infrastructure.
    """
    study_date   = str(getattr(ds, "StudyDate", "UNKNOWN"))
    study_uid    = str(getattr(ds, "StudyInstanceUID",  "unknown-study"))
    series_uid   = str(getattr(ds, "SeriesInstanceUID", "unknown-series"))
    instance_uid = str(ds.SOPInstanceUID)
    return f"{study_date}/{study_uid}/{series_uid}/{instance_uid}.dcm"


# ── DICOM validation ──────────────────────────────────────────────────────────

def validate(ds, path: str) -> tuple[bool, str]:
    """Basic sanity checks. Returns (ok, reason)."""

    # Must have the mandatory UIDs
    for tag in ("SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID"):
        if not hasattr(ds, tag):
            return False, f"Missing {tag}"

    # Must have pixel data
    if not hasattr(ds, "PixelData"):
        return False, "No PixelData"

    # Size guard
    if MAX_BYTES > 0:
        size = Path(path).stat().st_size
        if size > MAX_BYTES:
            return False, f"File {size} bytes exceeds limit {MAX_BYTES}"

    # Mammography-specific: warn (not reject) if laterality/view missing
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
    Runs synchronously inside the association thread — keep it fast.
    Heavy lifting (upload) happens here but could be queued for async in v2.
    """
    ds          = event.dataset
    ds.file_meta = event.file_meta  # attach file meta for transfer syntax access
    sending_ae  = event.assoc.requestor.ae_title.strip()

    instance_uid = str(ds.SOPInstanceUID)
    logger.info(f"Receiving  {instance_uid}  from  [{sending_ae}]")

    # Write to a temp file first (we want the raw bytes exactly as received)
    tmp_dir  = tempfile.mkdtemp(prefix="dcm_ingest_")
    tmp_path = os.path.join(tmp_dir, f"{instance_uid}.dcm")

    try:
        # Save with pydicom — preserves original transfer syntax, no re-encode
        pydicom.dcmwrite(tmp_path, ds, write_like_original=True)

        # ── Validate ──────────────────────────────────────────────────────────
        ok, reason = validate(ds, tmp_path)
        if not ok:
            logger.error(f"Validation FAILED for {instance_uid}: {reason}")
            _quarantine(tmp_path, instance_uid, reason)
            return 0xA700  # C-STORE failure: out of resources (generic refusal)

        # ── Checksum ──────────────────────────────────────────────────────────
        checksum   = sha256_of_file(tmp_path)
        size_bytes = Path(tmp_path).stat().st_size
        logger.info(f"  SHA-256: {checksum}  size: {size_bytes:,} bytes")

        # ── Blob storage ──────────────────────────────────────────────────────
        blob_key = make_blob_key(ds)
        blob_uri = storage.store(tmp_path, blob_key)

        # ── Postgres index ────────────────────────────────────────────────────
        if db:
            try:
                patient_id = db.upsert_patient(ds)
                exam_id    = db.upsert_exam(ds, patient_id)
                series_id  = db.upsert_series(ds, exam_id)
                inst_id    = db.insert_instance(
                    ds, series_id, blob_key, blob_uri,
                    size_bytes, checksum,
                    sending_ae   = sending_ae,
                    receiving_ae = AE_TITLE,
                )
                if inst_id is None:
                    logger.warning(f"  Duplicate instance {instance_uid} — already in DB, blob overwritten")
                else:
                    logger.info(f"  DB record id={inst_id}")

                # ── Notify routing server (if configured) ─────────────────
                if inst_id and ROUTER_URL:
                    _notify_router(
                        instance_id   = inst_id,
                        instance_uid  = instance_uid,
                        modality      = str(getattr(ds, "Modality", "") or ""),
                        sending_ae    = sending_ae,
                        receiving_ae  = AE_TITLE,
                        body_part     = str(getattr(ds, "BodyPartExamined", "") or ""),
                    )

            except Exception as db_err:
                # DB failure is logged but does NOT fail the C-STORE —
                # the file is safely in blob storage; DB can be repaired later.
                logger.error(f"  DB write failed (file is safe in storage): {db_err}")

        _instance_counter["since_last"] += 1
        logger.info(f"  ✓ Stored  {blob_key}")
        return 0x0000  # C-STORE success

    except Exception as e:
        logger.exception(f"Unexpected error processing {instance_uid}: {e}")
        _quarantine(tmp_path, instance_uid, str(e))
        return 0xA700  # failure

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _quarantine(src_path: str, uid: str, reason: str):
    """Move a problem file to the quarantine folder for manual review."""
    dest = QUARANTINE / f"{uid}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.dcm"
    try:
        shutil.move(src_path, dest)
        logger.warning(f"  → Quarantined {dest}  reason: {reason}")
    except Exception as e:
        logger.error(f"  Could not quarantine {src_path}: {e}")


# ── Router notification ───────────────────────────────────────────────────────

def _notify_router(instance_id: int, instance_uid: str,
                   modality: str, sending_ae: str,
                   receiving_ae: str, body_part: str):
    """
    POST to the server's /internal/routed endpoint so on_receive routing
    rules fire immediately. Non-blocking best-effort — failure is logged
    but never propagates to the C-STORE response.
    The 30-second queue processor on the server is always the safety net.
    """
    if not ROUTER_URL:
        return
    payload = _json.dumps({
        "instance_id":  instance_id,
        "instance_uid": instance_uid,
        "modality":     modality,
        "sending_ae":   sending_ae,
        "receiving_ae": receiving_ae,
        "body_part":    body_part,
    }).encode()
    try:
        req = urllib.request.Request(
            f"{ROUTER_URL}/internal/routed",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = _json.loads(resp.read())
            if result.get("routes_queued", 0):
                logger.info(
                    f"  Router notified — {result['routes_queued']} route(s) triggered"
                )
    except urllib.error.URLError as e:
        logger.warning(f"  Router notification failed (queue will retry): {e}")
    except Exception as e:
        logger.warning(f"  Router notification error: {e}")


# ── C-ECHO handler (ping/verification) ───────────────────────────────────────

def handle_echo(event):
    """Respond to C-ECHO so the modality can verify connectivity."""
    logger.info(f"C-ECHO from [{event.assoc.requestor.ae_title.strip()}]")
    return 0x0000


# ── Build and start the AE ────────────────────────────────────────────────────

def run():
    ae = AE(ae_title=AE_TITLE)

    # Accept C-ECHO (Verification)
    ae.add_supported_context(Verification)

    # Accept ALL storage SOP classes with ALL transfer syntaxes
    # This means we accept whatever the modality sends — no negotiation failures.
    # The raw bytes are preserved exactly as transmitted.
    for cx in AllStoragePresentationContexts:
        ae.add_supported_context(cx.abstract_syntax, ALL_TRANSFER_SYNTAXES)

    handlers = [
        (evt.EVT_C_STORE, handle_store),
        (evt.EVT_C_ECHO,  handle_echo),
    ]

    logger.info(f"╔══════════════════════════════════════════╗")
    logger.info(f"║  DICOM Archive Agent starting            ║")
    logger.info(f"║  AE Title : {AE_TITLE:<30}║")
    logger.info(f"║  Listening: {LISTEN_HOST}:{LISTEN_PORT:<26}║")
    logger.info(f"║  Storage  : {os.getenv('STORAGE_BACKEND','local'):<30}║")
    logger.info(f"║  Database : {'enabled' if db else 'disabled (file-only mode)':<30}║")
    logger.info(f"║  Router   : {ROUTER_URL or 'not configured':<30}║")
    logger.info(f"╚══════════════════════════════════════════╝")

    # Register with server and start heartbeat thread (best-effort)
    if ROUTER_URL:
        register_with_server()
        global _instance_counter
        _instance_counter = _heartbeat_loop(interval=60)

    ae.start_server(
        (LISTEN_HOST, LISTEN_PORT),
        evt_handlers=handlers,
        block=True,       # run forever
    )


if __name__ == "__main__":
    run()
