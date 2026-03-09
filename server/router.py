"""
router.py — Routing engine
Evaluates routing rules against newly ingested instances and forwards
them to destination AEs via C-STORE SCU.
Also provides manual re-route capability.
"""

import os
import sys
import logging
import tempfile
import shutil
from pathlib import Path
from typing import Optional

import pydicom
from pynetdicom import AE
from pynetdicom.sop_class import (
    DigitalMammographyXRayImageStorageForPresentation,
    DigitalMammographyXRayImageStorageForProcessing,
)
from pynetdicom import AllStoragePresentationContexts, ALL_TRANSFER_SYNTAXES

logger = logging.getLogger(__name__)

# Add agent path so we can reuse storage.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agent'))
from storage import get_storage_backend, StorageBackend


class Router:
    def __init__(self, db, storage: StorageBackend):
        self.db = db
        self.storage = storage

    def evaluate_and_queue(self, instance_id: int, modality: str,
                           sending_ae: str, body_part: str):
        """
        Called after a new instance is ingested.
        Finds matching routing rules and queues log entries.
        """
        rules = self.db.get_matching_rules(
            modality or "",
            sending_ae or "",
            body_part or ""
        )
        if not rules:
            logger.debug(f"No routing rules matched for instance {instance_id}")
            return 0

        queued = 0
        for rule in rules:
            log_id = self.db.log_route(instance_id, rule["id"], rule["destination_id"])
            logger.info(
                f"Queued route: instance={instance_id} → "
                f"{rule['dest_name']} [{rule['dest_ae']}] (rule: {rule['name']}, log={log_id})"
            )
            queued += 1
        return queued

    def process_queue(self):
        """
        Pick up all queued/failed routes and attempt to send them.
        Called periodically by a background task.
        """
        pending = self.db.get_pending_routes()
        if not pending:
            return

        logger.info(f"Processing {len(pending)} pending route(s)")
        for row in pending:
            self._send_instance(row)

    def route_instance_to_destination(self, instance_uid: str, destination_id: int) -> dict:
        """
        Manual route: send a specific instance to a specific destination.
        Returns status dict.
        """
        instance = self.db.get_instance(instance_uid)
        if not instance:
            return {"ok": False, "error": "Instance not found"}

        dest = self.db.get_destination(destination_id)
        if not dest:
            return {"ok": False, "error": "Destination not found"}

        # Create a one-off log entry
        log_id = self.db.log_route(instance["id"], None, destination_id, status="queued")

        row = {
            "log_id":       log_id,
            "instance_id":  instance["id"],
            "destination_id": destination_id,
            "blob_key":     instance["blob_key"],
            "blob_uri":     instance["blob_uri"],
            "instance_uid": instance_uid,
            "ae_title":     dest["ae_title"],
            "host":         dest["host"],
            "port":         dest["port"],
            "dest_name":    dest["name"],
        }
        return self._send_instance(row)

    def route_study_to_destination(self, study_uid: str, destination_id: int) -> dict:
        """Manual route: send all instances of a study to a destination."""
        series_list = self.db.get_series_for_study(study_uid)
        results = {"queued": 0, "success": 0, "failed": 0, "errors": []}
        for series in series_list:
            instances = self.db.get_instances_for_series(series["series_uid"])
            for inst in instances:
                r = self.route_instance_to_destination(inst["instance_uid"], destination_id)
                if r.get("ok"):
                    results["success"] += 1
                else:
                    results["failed"] += 1
                    results["errors"].append(r.get("error"))
        return results

    # ── Internal ──────────────────────────────────────────────────────────────

    def _send_instance(self, row: dict) -> dict:
        log_id      = row["log_id"]
        instance_uid = row["instance_uid"]
        ae_title    = row["ae_title"]
        host        = row["host"]
        port        = int(row["port"])
        dest_name   = row["dest_name"]

        logger.info(f"Sending {instance_uid} → {dest_name} [{ae_title}@{host}:{port}]")
        self.db.update_route_log(log_id, "sending")

        tmp_dir = tempfile.mkdtemp(prefix="dcm_route_")
        tmp_path = os.path.join(tmp_dir, f"{instance_uid}.dcm")

        try:
            # Fetch from blob storage
            self._fetch_to_local(row["blob_key"], tmp_path)

            # Read and send
            ds = pydicom.dcmread(tmp_path)
            success, error = self._cstore(ds, tmp_path, ae_title, host, port)

            if success:
                self.db.update_route_log(log_id, "success")
                logger.info(f"  ✓ Sent {instance_uid} → {dest_name}")
                return {"ok": True}
            else:
                self.db.update_route_log(log_id, "failed", error)
                logger.error(f"  ✗ Failed {instance_uid} → {dest_name}: {error}")
                return {"ok": False, "error": error}

        except Exception as e:
            err = str(e)
            self.db.update_route_log(log_id, "failed", err)
            logger.exception(f"  ✗ Exception routing {instance_uid}: {e}")
            return {"ok": False, "error": err}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _fetch_to_local(self, blob_key: str, dest_path: str):
        """Retrieve a blob to a local temp file, regardless of storage backend."""
        from storage import LocalStorage, S3Storage, AzureStorage

        if isinstance(self.storage, LocalStorage):
            src = self.storage.base / blob_key
            shutil.copy2(src, dest_path)

        elif isinstance(self.storage, S3Storage):
            bucket = self.storage.bucket
            self.storage.s3.download_file(bucket, blob_key, dest_path)

        elif isinstance(self.storage, AzureStorage):
            blob = self.storage.client.get_blob_client(
                container=self.storage.container, blob=blob_key
            )
            with open(dest_path, "wb") as f:
                data = blob.download_blob()
                data.readinto(f)
        else:
            raise ValueError(f"Unknown storage type: {type(self.storage)}")

    def _cstore(self, ds, file_path: str,
                ae_title: str, host: str, port: int) -> tuple[bool, Optional[str]]:
        """Send a DICOM file to a remote AE via C-STORE."""
        ae = AE(ae_title="ARCHIVE_SCU")

        # Request the SOP class from the dataset
        sop_class = str(ds.SOPClassUID)
        # Add with all transfer syntaxes for maximum compatibility
        ae.add_requested_context(sop_class, ALL_TRANSFER_SYNTAXES)

        assoc = ae.associate(host, port, ae_title=ae_title)
        if not assoc.is_established:
            return False, f"Could not establish association to {ae_title}@{host}:{port}"

        try:
            status = assoc.send_c_store(ds)
            if status and status.Status == 0x0000:
                return True, None
            else:
                code = f"0x{status.Status:04X}" if status else "no response"
                return False, f"C-STORE returned status {code}"
        finally:
            assoc.release()
