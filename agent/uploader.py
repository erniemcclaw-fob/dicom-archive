"""
uploader.py — Async upload engine for the 3-step ingest handshake.

Steps per file:
  1. POST /ingest/prepare  → get upload URL + instance_id
  2. PUT  .dcm to blob     → direct upload via pre-signed URL
  3. POST /ingest/confirm  → mark stored, trigger routing
  4. Delete local staging file
"""

import os
import asyncio
import logging
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger("dicom-agent.uploader")

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class UploadEngine:
    def __init__(self, server_url: str, api_key: str, workers: int = 4):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.workers = workers
        self.queue: asyncio.Queue = asyncio.Queue()
        self._session: aiohttp.ClientSession | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        self._session = aiohttp.ClientSession(
            headers={
                "X-Api-Key": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        for i in range(self.workers):
            task = asyncio.create_task(self._worker(i), name=f"upload-worker-{i}")
            self._tasks.append(task)
        logger.info("Upload engine started with %d workers", self.workers)

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        if self._session:
            await self._session.close()

    async def enqueue(self, local_path: str, metadata: dict):
        """Called by C-STORE handler after validation + local save."""
        await self.queue.put((local_path, metadata))

    async def _worker(self, worker_id: int):
        while True:
            try:
                local_path, metadata = await self.queue.get()
                await self._process(local_path, metadata, worker_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Worker %d unexpected error: %s", worker_id, e)
            finally:
                self.queue.task_done()

    async def _process(self, local_path: str, metadata: dict, worker_id: int):
        instance_uid = metadata.get("instance_uid", "?")
        logger.info("[W%d] Processing %s", worker_id, instance_uid)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # ① Prepare — get upload URL
                prep = await self._prepare(metadata)
                if not prep:
                    logger.error("[W%d] Prepare failed for %s (attempt %d/%d)",
                                 worker_id, instance_uid, attempt, MAX_RETRIES)
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    continue

                # ② Upload .dcm directly to blob storage
                uploaded = await self._upload(local_path, prep["upload_url"])
                if not uploaded:
                    logger.error("[W%d] Upload failed for %s (attempt %d/%d)",
                                 worker_id, instance_uid, attempt, MAX_RETRIES)
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    continue

                # ③ Confirm
                confirmed = await self._confirm(prep["instance_id"], metadata["sha256"])
                if not confirmed:
                    logger.error("[W%d] Confirm failed for %s (attempt %d/%d)",
                                 worker_id, instance_uid, attempt, MAX_RETRIES)
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    continue

                # ④ Delete local staging file
                try:
                    os.unlink(local_path)
                    logger.info("[W%d] ✓ Uploaded and confirmed %s", worker_id, instance_uid)
                except OSError as e:
                    logger.warning("[W%d] Could not delete staging file %s: %s",
                                   worker_id, local_path, e)
                return  # success

            except Exception as e:
                logger.exception("[W%d] Error processing %s (attempt %d/%d): %s",
                                 worker_id, instance_uid, attempt, MAX_RETRIES, e)
                await asyncio.sleep(RETRY_DELAY * attempt)

        logger.error("[W%d] Exhausted retries for %s — file remains at %s",
                     worker_id, instance_uid, local_path)

    async def _prepare(self, metadata: dict) -> dict | None:
        assert self._session
        try:
            async with self._session.post(
                f"{self.server_url}/ingest/prepare",
                json=metadata,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Prepare returned %d: %s", resp.status, body)
                    return None
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning("Prepare rejected: %s", data)
                    return None
                return data
        except Exception as e:
            logger.warning("Prepare request failed: %s", e)
            return None

    async def _upload(self, local_path: str, upload_url: str) -> bool:
        """PUT the .dcm file to the upload URL (server-proxied or pre-signed)."""
        try:
            file_size = os.path.getsize(local_path)
            with open(local_path, "rb") as f:
                headers = {
                    "Content-Type": "application/dicom",
                    "Content-Length": str(file_size),
                    "X-Api-Key": self.api_key,
                }
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as upload_session:
                    async with upload_session.put(
                        upload_url,
                        data=f,
                        headers=headers,
                    ) as resp:
                        if resp.status in (200, 201):
                            return True
                        body = await resp.text()
                        logger.warning("Upload returned %d: %s", resp.status, body[:500])
                        return False
        except Exception as e:
            logger.warning("Upload failed: %s", e)
            return False

    async def _confirm(self, instance_id: int, sha256: str) -> bool:
        assert self._session
        try:
            async with self._session.post(
                f"{self.server_url}/ingest/confirm",
                json={"instance_id": instance_id, "sha256": sha256},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Confirm returned %d: %s", resp.status, body)
                    return False
                data = await resp.json()
                if data.get("routes_queued", 0) > 0:
                    logger.info("  Router: %d route(s) queued", data["routes_queued"])
                return data.get("ok", False)
        except Exception as e:
            logger.warning("Confirm request failed: %s", e)
            return False
