import sqlite3
import hashlib
import hmac
import os
import json
from typing import Optional, Tuple, Union, Dict, Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "bot.db"))

def hash_student_id(raw_id: Optional[str]) -> Optional[str]:
    """
    Hashes student ID using HMAC-SHA256 with pepper to avoid storing PII.
    Strips any whitespace before hashing.
    If raw_id is None, returns None.
    """
    if raw_id is None:
        return None
    
    pepper_env = os.getenv("HMAC_SECRET_PEPPER")
    if not pepper_env:
        # Check backward compatible / test environment pepper
        pepper_env = os.getenv("APP_SECRET_PEPPER")
        
    if not pepper_env or len(pepper_env) < 32:
        raise ValueError("HMAC_SECRET_PEPPER is missing or under 32 characters in length.")
        
    pepper = pepper_env.encode('utf-8')
    return hmac.new(pepper, raw_id.strip().encode('utf-8'), hashlib.sha256).hexdigest()

def _connect() -> sqlite3.Connection:
    """
    Helper to get a database connection with foreign keys and WAL mode enabled.
    """
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def checkpoint_db() -> None:
    """
    Truncates WAL file to ensure bot.db-wal size remains <1MB on disk.
    """
    with _connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

def _normalize_discord_id(discord_id: Union[int, str]) -> str:
    if discord_id is None:
        return ""
    return str(discord_id)

def _parse_discord_id(d_id: Any) -> Union[int, str]:
    if d_id is None:
        return ""
    try:
        return int(d_id)
    except (ValueError, TypeError):
        return str(d_id)

def init_db() -> None:
    """
    Initializes the database and creates the verified_students table in STRICT mode.
    """
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA wal_autocheckpoint = 100;")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verified_students (
                discord_id TEXT PRIMARY KEY,
                student_hash TEXT UNIQUE,
                student_id_hash TEXT UNIQUE,
                verified_at TEXT,
                student_name TEXT,
                program_year TEXT,
                school_year_term TEXT,
                document_date TEXT,
                strikes INTEGER DEFAULT 0,
                is_locked INTEGER DEFAULT 0
            ) STRICT;
        """)
        for col, col_type in [
            ("student_hash", "TEXT"),
            ("student_id_hash", "TEXT"),
            ("student_name", "TEXT"),
            ("program_year", "TEXT"),
            ("school_year_term", "TEXT"),
            ("document_date", "TEXT"),
            ("strikes", "INTEGER"),
            ("is_locked", "INTEGER")
        ]:
            try:
                cursor.execute(f"ALTER TABLE verified_students ADD COLUMN {col} {col_type};")
            except sqlite3.OperationalError:
                pass
        conn.commit()

def is_student_registered(sha256_hash: str) -> bool:
    """
    Checks if a student hash exists in the database.
    """
    if not sha256_hash:
        return False
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT student_hash, student_id_hash FROM verified_students WHERE student_hash = ? OR student_id_hash = ?", (sha256_hash, sha256_hash))
        rows = cursor.fetchall()
        for row in rows:
            if row[0] is not None and hmac.compare_digest(row[0], sha256_hash):
                return True
            if row[1] is not None and hmac.compare_digest(row[1], sha256_hash):
                return True
        return False

def register_student(
    discord_id: Union[int, str],
    sha256_hash: str,
    student_name: Optional[str] = None,
    program_year: Optional[str] = None,
    school_year_term: Optional[str] = None,
    document_date: Optional[str] = None
) -> None:
    """
    Registers or updates a student in the verified_students table.
    Resets strikes and is_locked on conflict.
    """
    d_id_str = _normalize_discord_id(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO verified_students (discord_id, student_hash, student_id_hash, verified_at, student_name, program_year, school_year_term, document_date, strikes, is_locked)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(discord_id) DO UPDATE SET
                student_hash = excluded.student_hash,
                student_id_hash = COALESCE(excluded.student_id_hash, student_id_hash),
                verified_at = CURRENT_TIMESTAMP,
                student_name = COALESCE(excluded.student_name, student_name),
                program_year = COALESCE(excluded.program_year, program_year),
                school_year_term = COALESCE(excluded.school_year_term, school_year_term),
                document_date = COALESCE(excluded.document_date, document_date),
                strikes = 0,
                is_locked = 0
        """, (d_id_str, sha256_hash, sha256_hash, student_name, program_year, school_year_term, document_date))
        conn.commit()

def unlink_student(identifier: str) -> bool:
    """
    Removes a student verification record from the verified_students table.
    Accepts either discord_id or sha256_hash.
    """
    if not identifier:
        return False
    id_str = str(identifier)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, student_hash, student_id_hash FROM verified_students")
        rows = cursor.fetchall()
        target_discord_id = None
        for row in rows:
            if str(row[0]) == id_str or (row[1] and hmac.compare_digest(row[1], id_str)) or (row[2] and hmac.compare_digest(row[2], id_str)):
                target_discord_id = row[0]
                break
                
        if target_discord_id is not None:
            cursor.execute("DELETE FROM verified_students WHERE discord_id = ?", (target_discord_id,))
            conn.commit()
            return True
            
        cursor.execute("SELECT discord_id FROM verified_students WHERE discord_id = ? OR student_hash = ? OR student_id_hash = ?", (id_str, id_str, id_str))
        row = cursor.fetchone()
        if row:
            cursor.execute("DELETE FROM verified_students WHERE discord_id = ?", (row[0],))
            conn.commit()
            return True
        return False

# ==================== BACKWARD COMPATIBLE WRAPPERS ====================

def add_verified_user(
    discord_id: Union[int, str],
    student_id_hash: str,
    student_name: Optional[str] = None,
    program_year: Optional[str] = None,
    school_year_term: Optional[str] = None,
    document_date: Optional[str] = None
) -> bool:
    """
    Saves a successful verification using UPSERT to resolve primary key collisions.
    """
    if student_id_hash and len(student_id_hash) == 64 and all(c in '0123456789abcdefABCDEF' for c in student_id_hash):
        hashed_id = student_id_hash
    else:
        hashed_id = hash_student_id(student_id_hash)
    
    d_id_str = _normalize_discord_id(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id FROM verified_students WHERE student_id_hash = ?", (hashed_id,))
        row = cursor.fetchone()
        if row and str(row[0]) != str(d_id_str):
            return False  # Genuine duplicate: Another user owns this Student ID

        cursor.execute("""
            INSERT INTO verified_students (
                discord_id, student_id_hash, student_name, program_year, school_year_term, document_date
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                student_id_hash = excluded.student_id_hash,
                student_name = excluded.student_name,
                program_year = excluded.program_year,
                school_year_term = excluded.school_year_term,
                document_date = excluded.document_date;
        """, (d_id_str, hashed_id, student_name, program_year, school_year_term, document_date))
        try:
            cursor.execute("""
                UPDATE verified_students
                SET student_hash = student_id_hash,
                    verified_at = CURRENT_TIMESTAMP
                WHERE discord_id = ?
            """, (d_id_str,))
        except sqlite3.OperationalError:
            pass
        conn.commit()
        return True

def is_user_verified(discord_id: Union[int, str]) -> bool:
    """
    Returns True if a valid verified hash exists for the discord_id, False otherwise.
    """
    if discord_id is None:
        return False
    d_id_str = _normalize_discord_id(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT student_id_hash FROM verified_students WHERE discord_id = ? AND student_id_hash IS NOT NULL;", (d_id_str,))
        row = cursor.fetchone()
        return row is not None and row[0] is not None

def is_student_id_used(student_id: str) -> bool:
    """
    Returns True if the student_id is registered (wrapper around is_student_registered).
    """
    if not student_id:
        return False
    hashed_id = hash_student_id(student_id)
    return is_student_registered(hashed_id)

def is_student_id_used_by_other(student_id: str, discord_id: Union[int, str]) -> bool:
    """
    Returns True if student_id is registered to a different discord_id.
    """
    if not student_id:
        return False
    hashed_id = hash_student_id(student_id)
    d_id_str = _normalize_discord_id(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT student_hash, student_id_hash FROM verified_students WHERE discord_id != ?", (d_id_str,))
        rows = cursor.fetchall()
        for row in rows:
            if (row[0] and hmac.compare_digest(row[0], hashed_id)) or (row[1] and hmac.compare_digest(row[1], hashed_id)):
                return True
        return False

def get_user_state(discord_id: Union[int, str]) -> Optional[Tuple[int, bool]]:
    """
    Returns the user's strike count and lock status from verified_students.
    Returns (strikes, is_locked) or None if the user does not exist.
    """
    if discord_id is None:
        return None
    d_id_str = _normalize_discord_id(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT strikes, is_locked FROM verified_students WHERE discord_id = ?", (d_id_str,))
        row = cursor.fetchone()
        if row is not None:
            return row[0] or 0, bool(row[1]) if row[1] is not None else False
        return None

def add_strike(discord_id: Union[int, str]) -> None:
    """
    Increments the user's strike count in verified_students.
    If strikes reach 2, sets is_locked to True.
    Creates record with 1 strike if user does not exist.
    """
    if discord_id is None:
        return
    d_id_str = _normalize_discord_id(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT strikes FROM verified_students WHERE discord_id = ?", (d_id_str,))
        row = cursor.fetchone()
        if row is None:
            cursor.execute("""
                INSERT INTO verified_students (discord_id, strikes, is_locked)
                VALUES (?, 1, 0)
            """, (d_id_str,))
        else:
            current_strikes = row[0] if row[0] is not None else 0
            new_strikes = current_strikes + 1
            is_locked = 1 if new_strikes >= 2 else 0
            cursor.execute("""
                UPDATE verified_students
                SET strikes = ?, is_locked = ?
                WHERE discord_id = ?
            """, (new_strikes, is_locked, d_id_str))
        conn.commit()

def unlock_user(discord_id: Union[int, str]) -> None:
    """
    Resets strikes to 0 and is_locked to False in verified_students (for staff use).
    Does nothing if the user does not exist.
    """
    if discord_id is None:
        return
    d_id_str = _normalize_discord_id(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE verified_students
            SET strikes = 0, is_locked = 0
            WHERE discord_id = ?
        """, (d_id_str,))
        conn.commit()

def get_student_by_id(student_id: str) -> Optional[Dict[str, Any]]:
    """
    Hash incoming student_id and return dict with student_id_hash, discord_id (parsed), timestamp,
    and credential fields, or None if not found.
    """
    if not student_id:
        return None
    hashed_id = hash_student_id(student_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT student_hash, student_id_hash, discord_id, verified_at, student_name, program_year, school_year_term, document_date FROM verified_students")
        rows = cursor.fetchall()
        for row in rows:
            s_hash = row[0]
            s_id_hash = row[1]
            if (s_hash and (hmac.compare_digest(s_hash, hashed_id) or hmac.compare_digest(s_hash, student_id))) or \
               (s_id_hash and (hmac.compare_digest(s_id_hash, hashed_id) or hmac.compare_digest(s_id_hash, student_id))):
                d_id = _parse_discord_id(row[2])
                return {
                    "student_id_hash": s_id_hash or s_hash,
                    "discord_id": d_id,
                    "timestamp": row[3],
                    "student_name": row[4],
                    "program_year": row[5],
                    "school_year_term": row[6],
                    "document_date": row[7]
                }
        return None

def get_student_by_discord_id(discord_id: Union[int, str]) -> Optional[Dict[str, Any]]:
    """
    Query verified_students table by discord_id and return dict with student_id_hash, discord_id (parsed), timestamp,
    and credential fields, or None if not found.
    """
    if discord_id is None:
        return None
    d_id_str = _normalize_discord_id(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT student_id_hash, student_hash, discord_id, verified_at, student_name, program_year, school_year_term, document_date FROM verified_students WHERE discord_id = ?",
            (d_id_str,)
        )
        row = cursor.fetchone()
        if row and (row[0] or row[1]):
            s_id_hash = row[0] or row[1]
            d_id = _parse_discord_id(row[2])
            return {
                "student_id_hash": s_id_hash,
                "discord_id": d_id,
                "timestamp": row[3],
                "student_name": row[4],
                "program_year": row[5],
                "school_year_term": row[6],
                "document_date": row[7]
            }
        return None

def delete_student_record(student_id: str) -> bool:
    """
    Hash incoming student_id and delete record matching student_id.
    Returns True if a row was deleted, False otherwise.
    """
    if not student_id:
        return False
    hashed_id = hash_student_id(student_id)
    return unlink_student(hashed_id) or unlink_student(student_id)

def delete_student_by_discord_id(discord_id: Union[int, str]) -> bool:
    """
    Delete record matching discord_id.
    Returns True if a row was deleted, False otherwise.
    """
    if discord_id is None:
        return False
    return unlink_student(str(discord_id))

def reset_all_locks():
    with _connect() as conn:
        conn.execute("UPDATE verified_students SET strikes = 0, is_locked = 0;")
        conn.commit()
        print("[DATABASE] All user strikes and lockouts have been reset to 0.", flush=True)

def update_student_details(
    discord_id: Union[int, str],
    student_name: str,
    program_year: str,
    school_year_term: str,
    document_date: str
) -> bool:
    """Updates student metadata in the verified_students table."""
    d_id_str = _normalize_discord_id(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE verified_students
            SET student_name = ?, program_year = ?, school_year_term = ?, document_date = ?
            WHERE discord_id = ?;
            """,
            (student_name, program_year, school_year_term, document_date, d_id_str)
        )
        conn.commit()
        return cursor.rowcount > 0
