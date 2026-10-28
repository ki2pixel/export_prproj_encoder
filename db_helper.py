"""Persistance SQLite des soumissions et de leur statut d'export.

Toutes les connexions sont ouvertes avec `contextlib.closing` + `with conn:` pour un accès
thread-safe sans fuite (l'application, l'encodeur et le moniteur tournent dans le même process).
"""
import sqlite3
from pathlib import Path
from contextlib import closing

from config import MATCHABLE_STATUSES, STATUS_SUBMITTED

_COLUMNS = (
    "timestamp",
    "status",
    "original_file_path",
    "watchfolder_file_name",
    "expected_export_name",
    "job_id",
)


def get_db_connection(db_path: Path):
    """Retourne une connexion à la base SQLite (timeout large pour absorber les accès concurrents)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path):
    """
    Initialise la base et crée la table `submissions` si nécessaire.
    Gère la migration automatique si la colonne `job_id` est absente.
    """
    with closing(get_db_connection(db_path)) as conn:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    original_file_path TEXT NOT NULL,
                    watchfolder_file_name TEXT NOT NULL,
                    expected_export_name TEXT NOT NULL,
                    job_id TEXT
                )
            """)
            # Index pour accélérer la recherche des jobs en attente
            conn.execute("CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status)")

            # Migration : ajout de job_id si la base a été créée avant son introduction
            cursor = conn.execute("PRAGMA table_info(submissions)")
            columns = [row['name'] for row in cursor.fetchall()]
            if 'job_id' not in columns:
                conn.execute("ALTER TABLE submissions ADD COLUMN job_id TEXT")

            # Index unique sur job_id (recherche rapide + unicité)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_job_id ON submissions(job_id)")


def add_submission(db_path: Path, timestamp: str, original_file_path: str, watchfolder_file_name: str,
                   expected_export_name: str, job_id: str, status: str = STATUS_SUBMITTED):
    """Insère une nouvelle soumission avec son job_id unique."""
    with closing(get_db_connection(db_path)) as conn:
        with conn:
            conn.execute("""
                INSERT INTO submissions (timestamp, status, original_file_path, watchfolder_file_name, expected_export_name, job_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (timestamp, status, str(original_file_path), watchfolder_file_name, expected_export_name, job_id))


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        'Timestamp': row['timestamp'],
        'Status': row['status'],
        'OriginalFilePath': row['original_file_path'],
        'WatchfolderFileName': row['watchfolder_file_name'],
        'ExpectedExportName': row['expected_export_name'],
        'JobId': row['job_id'],
    }


def get_matchable_submissions(db_path: Path):
    """
    Retourne toutes les soumissions susceptibles d'être rapprochées d'un fichier exporté
    (statuts intermédiaires : soumise, en encodage, encodée).
    """
    statuses = sorted(MATCHABLE_STATUSES)
    placeholders = ", ".join("?" for _ in statuses)
    with closing(get_db_connection(db_path)) as conn:
        cursor = conn.execute(f"""
            SELECT timestamp, status, original_file_path, watchfolder_file_name, expected_export_name, job_id
            FROM submissions
            WHERE status IN ({placeholders})
        """, statuses)
        return [_row_to_dict(row) for row in cursor.fetchall()]


def get_submission_by_job_id(db_path: Path, job_id: str):
    """Retourne la soumission correspondant au job_id, ou None."""
    with closing(get_db_connection(db_path)) as conn:
        cursor = conn.execute("""
            SELECT timestamp, status, original_file_path, watchfolder_file_name, expected_export_name, job_id
            FROM submissions
            WHERE job_id = ?
        """, (job_id,))
        row = cursor.fetchone()
        return _row_to_dict(row) if row else None


def update_submission_status_by_job_id(db_path: Path, job_id: str, new_status: str) -> bool:
    """Met à jour le statut d'une soumission via son job_id. Retourne True si une ligne a été modifiée."""
    with closing(get_db_connection(db_path)) as conn:
        with conn:
            cursor = conn.execute("""
                UPDATE submissions
                SET status = ?
                WHERE job_id = ?
            """, (new_status, job_id))
            return cursor.rowcount > 0


def update_submission_status_if_current(db_path: Path, job_id: str, expected_status: str,
                                        new_status: str) -> bool:
    """
    Met à jour le statut uniquement si la soumission est encore dans `expected_status`.
    Transition atomique : évite d'écraser un job que l'encodeur vient de démarrer.
    """
    if not job_id:
        return False
    with closing(get_db_connection(db_path)) as conn:
        with conn:
            cursor = conn.execute("""
                UPDATE submissions
                SET status = ?
                WHERE job_id = ? AND status = ?
            """, (new_status, job_id, expected_status))
            return cursor.rowcount > 0


def update_submission_status(db_path: Path, matched_timestamp: str, original_file_path: str,
                             watchfolder_file_name: str, expected_export_name: str, new_status: str) -> bool:
    """
    Met à jour le statut d'une soumission correspondant aux détails fournis et encore en attente.
    Repli historique utilisé quand aucun job_id n'est disponible.
    """
    statuses = sorted(MATCHABLE_STATUSES)
    placeholders = ", ".join("?" for _ in statuses)
    with closing(get_db_connection(db_path)) as conn:
        with conn:
            cursor = conn.execute(f"""
                UPDATE submissions
                SET status = ?
                WHERE timestamp = ?
                  AND original_file_path = ?
                  AND watchfolder_file_name = ?
                  AND expected_export_name = ?
                  AND status IN ({placeholders})
            """, (new_status, matched_timestamp, str(original_file_path), watchfolder_file_name,
                  expected_export_name, *statuses))
            return cursor.rowcount > 0
