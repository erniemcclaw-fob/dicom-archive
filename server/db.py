"""
db.py — Database connection + all queries for the server
Shares the same Postgres instance as the ingest agent.
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional
import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS ae_destinations (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    ae_title     TEXT NOT NULL,
    host         TEXT NOT NULL,
    port         INTEGER NOT NULL DEFAULT 104,
    description  TEXT,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS routing_rules (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 100,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    -- Match criteria (NULL = wildcard / match anything)
    match_modality  TEXT,
    match_ae_title  TEXT,
    match_body_part TEXT,
    -- Behaviour
    on_receive      BOOLEAN NOT NULL DEFAULT FALSE,  -- auto-route on ingest
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Many-to-many: one rule can fan out to multiple destinations
CREATE TABLE IF NOT EXISTS rule_destinations (
    rule_id        INTEGER NOT NULL REFERENCES routing_rules(id)   ON DELETE CASCADE,
    destination_id INTEGER NOT NULL REFERENCES ae_destinations(id) ON DELETE CASCADE,
    PRIMARY KEY (rule_id, destination_id)
);

CREATE TABLE IF NOT EXISTS routing_log (
    id              SERIAL PRIMARY KEY,
    instance_id     INTEGER REFERENCES instances(id),
    rule_id         INTEGER REFERENCES routing_rules(id),
    destination_id  INTEGER REFERENCES ae_destinations(id),
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued|sending|success|failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    queued_at       TIMESTAMPTZ DEFAULT NOW(),
    sent_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_routing_log_status   ON routing_log(status);
CREATE INDEX IF NOT EXISTS idx_routing_log_instance ON routing_log(instance_id);

-- Migrate old single destination_id column if it existed
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='routing_rules' AND column_name='destination_id'
  ) THEN
    -- Migrate any existing single-destination rules to rule_destinations
    INSERT INTO rule_destinations (rule_id, destination_id)
    SELECT id, destination_id FROM routing_rules
    WHERE destination_id IS NOT NULL
    ON CONFLICT DO NOTHING;

    ALTER TABLE routing_rules DROP COLUMN IF EXISTS destination_id;
  END IF;
END $$;
"""


class DB:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: Optional[psycopg2.extensions.connection] = None

    def connect(self):
        self._conn = psycopg2.connect(self.dsn)
        self._conn.autocommit = False
        with self._conn.cursor() as cur:
            cur.execute(EXTRA_DDL)
        self._conn.commit()
        logger.info("DB connected and schema verified")

    @contextmanager
    def cursor(self):
        if not self._conn or self._conn.closed:
            self.connect()
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # ── Studies ───────────────────────────────────────────────────────────────

    def list_studies(self, patient_id=None, modality=None,
                     date_from=None, date_to=None, limit=100, offset=0):
        clauses, params = [], []
        if patient_id:
            clauses.append("p.patient_id ILIKE %s"); params.append(f"%{patient_id}%")
        if modality:
            clauses.append("e.modality = %s"); params.append(modality.upper())
        if date_from:
            clauses.append("e.study_date >= %s"); params.append(date_from)
        if date_to:
            clauses.append("e.study_date <= %s"); params.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.cursor() as cur:
            cur.execute(f"""
                SELECT e.id, e.study_uid, e.study_date, e.accession,
                       e.description, e.modality,
                       p.patient_id, p.name AS patient_name, p.birth_date,
                       COUNT(DISTINCT s.id) AS series_count,
                       COUNT(i.id)          AS instance_count
                FROM exams e
                JOIN patients p ON p.id = e.patient_id
                LEFT JOIN series s  ON s.exam_id   = e.id
                LEFT JOIN instances i ON i.series_id = s.id
                {where}
                GROUP BY e.id, p.id
                ORDER BY e.study_date DESC NULLS LAST, e.id DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            return cur.fetchall()

    def get_study(self, study_uid: str):
        with self.cursor() as cur:
            cur.execute("""
                SELECT e.*, p.patient_id, p.name AS patient_name,
                       p.birth_date, p.sex
                FROM exams e JOIN patients p ON p.id = e.patient_id
                WHERE e.study_uid = %s
            """, (study_uid,))
            return cur.fetchone()

    def get_series_for_study(self, study_uid: str):
        with self.cursor() as cur:
            cur.execute("""
                SELECT s.*, COUNT(i.id) AS instance_count
                FROM series s
                JOIN exams e ON e.id = s.exam_id
                LEFT JOIN instances i ON i.series_id = s.id
                WHERE e.study_uid = %s
                GROUP BY s.id
                ORDER BY s.series_number
            """, (study_uid,))
            return cur.fetchall()

    def get_instances_for_series(self, series_uid: str):
        with self.cursor() as cur:
            cur.execute("""
                SELECT i.*
                FROM instances i
                JOIN series s ON s.id = i.series_id
                WHERE s.series_uid = %s
                ORDER BY i.instance_number
            """, (series_uid,))
            return cur.fetchall()

    def get_instance(self, instance_uid: str):
        with self.cursor() as cur:
            cur.execute("""
                SELECT i.*, s.series_uid, s.laterality, s.view_position,
                       e.study_uid, e.modality, e.study_date,
                       p.patient_id, p.name AS patient_name
                FROM instances i
                JOIN series s   ON s.id  = i.series_id
                JOIN exams e    ON e.id  = s.exam_id
                JOIN patients p ON p.id  = e.patient_id
                WHERE i.instance_uid = %s
            """, (instance_uid,))
            return cur.fetchone()

    def get_instance_by_id(self, instance_id: int):
        with self.cursor() as cur:
            cur.execute("""
                SELECT i.*, s.series_uid, e.study_uid, e.modality,
                       s.body_part, p.patient_id
                FROM instances i
                JOIN series s   ON s.id = i.series_id
                JOIN exams e    ON e.id = s.exam_id
                JOIN patients p ON p.id = e.patient_id
                WHERE i.id = %s
            """, (instance_id,))
            return cur.fetchone()

    # ── Destinations ──────────────────────────────────────────────────────────

    def list_destinations(self):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM ae_destinations ORDER BY name")
            return cur.fetchall()

    def get_destination(self, dest_id: int):
        with self.cursor() as cur:
            cur.execute("SELECT * FROM ae_destinations WHERE id = %s", (dest_id,))
            return cur.fetchone()

    def create_destination(self, name, ae_title, host, port, description=None):
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO ae_destinations (name, ae_title, host, port, description)
                VALUES (%s, %s, %s, %s, %s) RETURNING *
            """, (name, ae_title.upper(), host, port, description))
            return cur.fetchone()

    def update_destination(self, dest_id, **fields):
        sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = NOW()"
        with self.cursor() as cur:
            cur.execute(
                f"UPDATE ae_destinations SET {sets} WHERE id = %s RETURNING *",
                list(fields.values()) + [dest_id]
            )
            return cur.fetchone()

    def delete_destination(self, dest_id: int):
        with self.cursor() as cur:
            cur.execute("DELETE FROM ae_destinations WHERE id = %s", (dest_id,))

    # ── Routing rules ─────────────────────────────────────────────────────────

    def list_rules(self):
        """Return rules with their destination list."""
        with self.cursor() as cur:
            cur.execute("""
                SELECT r.*,
                       COALESCE(
                         json_agg(
                           json_build_object(
                             'id', d.id, 'name', d.name,
                             'ae_title', d.ae_title, 'host', d.host,
                             'port', d.port, 'enabled', d.enabled
                           ) ORDER BY d.name
                         ) FILTER (WHERE d.id IS NOT NULL),
                       '[]'::json) AS destinations
                FROM routing_rules r
                LEFT JOIN rule_destinations rd ON rd.rule_id = r.id
                LEFT JOIN ae_destinations   d  ON d.id = rd.destination_id
                GROUP BY r.id
                ORDER BY r.priority, r.id
            """)
            return cur.fetchall()

    def get_rule(self, rule_id: int):
        with self.cursor() as cur:
            cur.execute("""
                SELECT r.*,
                       ARRAY_AGG(rd.destination_id) FILTER (WHERE rd.destination_id IS NOT NULL)
                         AS destination_ids
                FROM routing_rules r
                LEFT JOIN rule_destinations rd ON rd.rule_id = r.id
                WHERE r.id = %s
                GROUP BY r.id
            """, (rule_id,))
            return cur.fetchone()

    def create_rule(self, name, destination_ids: list, priority=100,
                    match_modality=None, match_ae_title=None,
                    match_body_part=None, on_receive=False, description=None):
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO routing_rules
                    (name, priority, match_modality, match_ae_title,
                     match_body_part, on_receive, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
            """, (name, priority, match_modality, match_ae_title,
                  match_body_part, on_receive, description))
            rule = cur.fetchone()
            self._set_rule_destinations(cur, rule["id"], destination_ids)
            return rule

    def update_rule(self, rule_id, destination_ids: list = None, **fields):
        sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = NOW()"
        with self.cursor() as cur:
            cur.execute(
                f"UPDATE routing_rules SET {sets} WHERE id = %s RETURNING *",
                list(fields.values()) + [rule_id]
            )
            rule = cur.fetchone()
            if destination_ids is not None:
                self._set_rule_destinations(cur, rule_id, destination_ids)
            return rule

    def _set_rule_destinations(self, cur, rule_id, destination_ids: list):
        cur.execute("DELETE FROM rule_destinations WHERE rule_id = %s", (rule_id,))
        for dest_id in destination_ids:
            cur.execute(
                "INSERT INTO rule_destinations (rule_id, destination_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (rule_id, dest_id)
            )

    def delete_rule(self, rule_id: int):
        with self.cursor() as cur:
            cur.execute("DELETE FROM routing_rules WHERE id = %s", (rule_id,))

    def get_matching_rules(self, modality: str, sending_ae: str, body_part: str):
        """
        Return enabled on_receive rules matching this instance's metadata,
        expanded to one row per destination.
        """
        with self.cursor() as cur:
            cur.execute("""
                SELECT r.id AS rule_id, r.name AS rule_name,
                       d.id AS destination_id,
                       d.name AS dest_name, d.ae_title AS dest_ae,
                       d.host AS dest_host, d.port AS dest_port
                FROM routing_rules r
                JOIN rule_destinations rd ON rd.rule_id = r.id
                JOIN ae_destinations   d  ON d.id = rd.destination_id
                WHERE r.enabled    = TRUE
                  AND r.on_receive = TRUE
                  AND d.enabled    = TRUE
                  AND (r.match_modality  IS NULL OR r.match_modality  = %s)
                  AND (r.match_ae_title  IS NULL OR r.match_ae_title  = %s)
                  AND (r.match_body_part IS NULL OR r.match_body_part = %s)
                ORDER BY r.priority, r.id
            """, (modality or None, sending_ae or None, body_part or None))
            return cur.fetchall()

    # ── Routing log ───────────────────────────────────────────────────────────

    def log_route(self, instance_id, rule_id, destination_id, status="queued"):
        with self.cursor() as cur:
            cur.execute("""
                INSERT INTO routing_log (instance_id, rule_id, destination_id, status)
                VALUES (%s, %s, %s, %s) RETURNING id
            """, (instance_id, rule_id, destination_id, status))
            return cur.fetchone()["id"]

    def update_route_log(self, log_id, status, error=None):
        with self.cursor() as cur:
            cur.execute("""
                UPDATE routing_log
                SET status = %s, last_error = %s, attempts = attempts + 1,
                    sent_at = CASE WHEN %s = 'success' THEN NOW() ELSE sent_at END
                WHERE id = %s
            """, (status, error, status, log_id))

    def list_routing_log(self, limit=50):
        with self.cursor() as cur:
            cur.execute("""
                SELECT rl.*, i.instance_uid,
                       d.name AS destination_name,
                       r.name AS rule_name
                FROM routing_log rl
                LEFT JOIN instances       i ON i.id  = rl.instance_id
                LEFT JOIN ae_destinations d ON d.id  = rl.destination_id
                LEFT JOIN routing_rules   r ON r.id  = rl.rule_id
                ORDER BY rl.queued_at DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()

    def get_pending_routes(self):
        with self.cursor() as cur:
            cur.execute("""
                SELECT rl.id AS log_id, rl.instance_id, rl.destination_id,
                       i.blob_key, i.blob_uri, i.instance_uid,
                       d.ae_title, d.host, d.port, d.name AS dest_name
                FROM routing_log rl
                JOIN instances       i ON i.id  = rl.instance_id
                JOIN ae_destinations d ON d.id  = rl.destination_id
                WHERE rl.status IN ('queued', 'failed')
                  AND rl.attempts < 3
                  AND d.enabled = TRUE
                ORDER BY rl.queued_at
            """)
            return cur.fetchall()

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_stats(self):
        with self.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM patients)  AS total_patients,
                    (SELECT COUNT(*) FROM exams)     AS total_studies,
                    (SELECT COUNT(*) FROM series)    AS total_series,
                    (SELECT COUNT(*) FROM instances) AS total_instances,
                    (SELECT COALESCE(SUM(size_bytes),0) FROM instances) AS total_bytes,
                    (SELECT COUNT(*) FROM routing_log WHERE status='success') AS routes_ok,
                    (SELECT COUNT(*) FROM routing_log WHERE status='failed')  AS routes_failed,
                    (SELECT COUNT(*) FROM routing_log WHERE status='queued')  AS routes_queued
            """)
            return cur.fetchone()


_db: Optional[DB] = None

def get_db() -> DB:
    global _db
    if _db is None:
        dsn = os.getenv("DATABASE_URL", "")
        if not dsn:
            raise RuntimeError("DATABASE_URL not set")
        _db = DB(dsn)
        _db.connect()
    return _db
