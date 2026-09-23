import sqlite3
import hashlib
import hmac
import os
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

def init_db() -> None:
    """
    Initializes the database and creates the tables in STRICT mode.
    """
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS verified_students (
                discord_id INTEGER PRIMARY KEY,
                student_hash TEXT NOT NULL UNIQUE,
                verified_at TEXT NOT NULL
            ) STRICT;
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id TEXT PRIMARY KEY,
                student_id TEXT UNIQUE,
                strikes INTEGER DEFAULT 0,
                is_locked INTEGER DEFAULT 0,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            ) STRICT;
        """)
        conn.commit()

def is_student_registered(sha256_hash: str) -> bool:
    """
    Checks if a student hash exists in the database.
    """
    if not sha256_hash:
        return False
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT student_hash FROM verified_students WHERE student_hash = ?", (sha256_hash,))
        row = cursor.fetchone()
        if row is not None:
            return hmac.compare_digest(row[0], sha256_hash)
        return False

def register_student(discord_id: str, sha256_hash: str) -> None:
    """
    Registers or updates a student in the database.
    Resets strikes and is_locked on conflict.
    """
    with _connect() as conn:
        cursor = conn.cursor()
        # Insert/update in verified_students
        cursor.execute("""
            INSERT INTO verified_students (discord_id, student_hash, verified_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(discord_id) DO UPDATE SET
                student_hash = excluded.student_hash,
                verified_at = CURRENT_TIMESTAMP
        """, (int(discord_id), sha256_hash))
        
        # Insert/update in users for strikes and lock compatibility
        cursor.execute("""
            INSERT INTO users (discord_id, student_id, strikes, is_locked, timestamp)
            VALUES (?, ?, 0, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(discord_id) DO UPDATE SET
                student_id = excluded.student_id,
                strikes = 0,
                is_locked = 0,
                timestamp = CURRENT_TIMESTAMP
        """, (str(discord_id), sha256_hash))
        conn.commit()

def unlink_student(identifier: str) -> bool:
    """
    Removes a student verification record from the database.
    Accepts either discord_id or sha256_hash.
    """
    if not identifier:
        return False
    with _connect() as conn:
        cursor = conn.cursor()
        # Find matching record from verified_students by constant-time check
        cursor.execute("SELECT discord_id, student_hash FROM verified_students")
        rows = cursor.fetchall()
        target_discord_id = None
        for row in rows:
            if str(row[0]) == str(identifier) or hmac.compare_digest(row[1], identifier):
                target_discord_id = row[0]
                break
                
        if target_discord_id is not None:
            cursor.execute("DELETE FROM verified_students WHERE discord_id = ?", (target_discord_id,))
            cursor.execute("DELETE FROM users WHERE discord_id = ?", (str(target_discord_id),))
            conn.commit()
            return True
            
        # Fallback check on users table
        cursor.execute("SELECT discord_id FROM users WHERE discord_id = ? OR student_id = ?", (str(identifier), str(identifier)))
        row = cursor.fetchone()
        if row:
            cursor.execute("DELETE FROM verified_students WHERE discord_id = ?", (int(row[0]),))
            cursor.execute("DELETE FROM users WHERE discord_id = ?", (row[0],))
            conn.commit()
            return True
        return False

# ==================== BACKWARD COMPATIBLE WRAPPERS ====================

def add_verified_user(discord_id: str, student_id: str) -> None:
    """
    Saves a successful verification (wrapper around register_student).
    """
    hashed_id = hash_student_id(student_id)
    register_student(discord_id, hashed_id)

def is_student_id_used(student_id: str) -> bool:
    """
    Returns True if the student_id is registered (wrapper around is_student_registered).
    """
    if not student_id:
        return False
    hashed_id = hash_student_id(student_id)
    return is_student_registered(hashed_id)

def is_student_id_used_by_other(student_id: str, discord_id: str) -> bool:
    """
    Returns True if student_id is registered to a different discord_id.
    """
    if not student_id:
        return False
    hashed_id = hash_student_id(student_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT student_hash FROM verified_students WHERE discord_id != ?", (int(discord_id),))
        rows = cursor.fetchall()
        for row in rows:
            if hmac.compare_digest(row[0], hashed_id):
                return True
        return False

def get_user_state(discord_id: str) -> Optional[Tuple[int, bool]]:
    """
    Returns the user's strike count and lock status.
    Returns (strikes, is_locked) or None if the user does not exist.
    """
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT strikes, is_locked FROM users WHERE discord_id = ?", (str(discord_id),))
        row = cursor.fetchone()
        if row is not None:
            return row[0], bool(row[1])
        return None

def add_strike(discord_id: str) -> None:
    """
    Increments the user's strike count.
    If strikes reach 2, sets is_locked to True.
    Creates user record with 1 strike if user does not exist.
    """
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT strikes FROM users WHERE discord_id = ?", (str(discord_id),))
        row = cursor.fetchone()
        if row is None:
            cursor.execute("""
                INSERT INTO users (discord_id, strikes, is_locked, timestamp)
                VALUES (?, 1, 0, CURRENT_TIMESTAMP)
            """, (str(discord_id),))
        else:
            new_strikes = row[0] + 1
            is_locked = 1 if new_strikes >= 2 else 0
            cursor.execute("""
                UPDATE users
                SET strikes = ?, is_locked = ?
                WHERE discord_id = ?
            """, (new_strikes, is_locked, str(discord_id)))
        conn.commit()

def unlock_user(discord_id: str) -> None:
    """
    Resets strikes to 0 and is_locked to False (for staff use).
    Does nothing if the user does not exist.
    """
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users
            SET strikes = 0, is_locked = 0
            WHERE discord_id = ?
        """, (str(discord_id),))
        conn.commit()

def get_student_by_id(student_id: str) -> Optional[Dict[str, Any]]:
    """
    Hash incoming student_id and return dict with student_id_hash, discord_id (int), and timestamp,
    or None if not found.
    """
    if not student_id:
        return None
    hashed_id = hash_student_id(student_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT student_hash, discord_id, verified_at FROM verified_students")
        rows = cursor.fetchall()
        for row in rows:
            if hmac.compare_digest(row[0], hashed_id) or hmac.compare_digest(row[0], student_id):
                d_id = row[1]
                try:
                    d_id = int(d_id)
                except (ValueError, TypeError):
                    pass
                return {
                    "student_id_hash": row[0],
                    "discord_id": d_id,
                    "timestamp": row[2]
                }
        return None

def get_student_by_discord_id(discord_id: Union[int, str]) -> Optional[Dict[str, Any]]:
    """
    Query table by discord_id and return dict with student_id_hash, discord_id (int), and timestamp,
    or None if not found.
    """
    if discord_id is None:
        return None
    d_int = int(discord_id)
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT student_hash, discord_id, verified_at FROM verified_students WHERE discord_id = ?",
            (d_int,)
        )
        row = cursor.fetchone()
        if row:
            d_id = row[1]
            try:
                d_id = int(d_id)
            except (ValueError, TypeError):
                pass
            return {
                "student_id_hash": row[0],
                "discord_id": d_id,
                "timestamp": row[2]
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
        conn.execute("UPDATE users SET strikes = 0, is_locked = 0;")
        conn.commit()
        print("[DATABASE] All user strikes and lockouts have been reset to 0.", flush=True)

