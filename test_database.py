import unittest
from unittest.mock import patch, MagicMock
import os
import gc
import sqlite3
import hashlib
import hmac
import database

def cleanup_db(db_name: str) -> None:
    gc.collect()
    for ext in ("", "-wal", "-shm"):
        path = f"{db_name}{ext}"
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

class TestDatabase(unittest.TestCase):
    def setUp(self) -> None:
        # Patch the database name to a test database file
        database.DB_NAME = "test_verified_students.db"
        # Setup valid environment pepper for tests
        os.environ["HMAC_SECRET_PEPPER"] = "default_secret_pepper_for_testing_only_32_characters_long"
        # Ensure a clean database for each test
        cleanup_db(database.DB_NAME)
        database.init_db()

    def tearDown(self) -> None:
        # Clean up test database file after each test
        cleanup_db(database.DB_NAME)

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup_db("test_verified_students.db")
        cleanup_db("verified_students.db")

    def test_init_db(self) -> None:
        # init_db is called in setUp, verify that tables exist and journal_mode is WAL
        with sqlite3.connect(database.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='verified_students'")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "verified_students")
            
            cursor.execute("PRAGMA journal_mode;")
            mode = cursor.fetchone()[0]
            self.assertEqual(mode.lower(), "wal")

    def test_is_student_registered(self) -> None:
        sha_hash = database.hash_student_id("123456789")
        self.assertFalse(database.is_student_registered(sha_hash))
        
        database.register_student("123456", sha_hash)
        self.assertTrue(database.is_student_registered(sha_hash))

    def test_register_student(self) -> None:
        sha_hash = database.hash_student_id("987654321")
        database.register_student("123456", sha_hash)
        
        state = database.get_user_state("123456")
        self.assertIsNotNone(state)
        # strikes, is_locked
        self.assertEqual(state, (0, False))

    def test_unlink_student(self) -> None:
        sha_hash = database.hash_student_id("555555555")
        database.register_student("1234567", sha_hash)
        self.assertTrue(database.is_student_registered(sha_hash))
        
        # Unlink by discord id
        success = database.unlink_student("1234567")
        self.assertTrue(success)
        self.assertFalse(database.is_student_registered(sha_hash))
        
        # Re-register and unlink by student_id_hash
        database.register_student("1234567", sha_hash)
        success = database.unlink_student(sha_hash)
        self.assertTrue(success)
        self.assertFalse(database.is_student_registered(sha_hash))

    def test_add_verified_user_new(self) -> None:
        database.add_verified_user("123456", "student456")
        state = database.get_user_state("123456")
        self.assertIsNotNone(state)
        self.assertEqual(state, (0, False))
        
        # Check student id usage
        self.assertTrue(database.is_student_id_used("student456"))
        self.assertFalse(database.is_student_id_used("nonexistent"))

    def test_add_verified_user_existing(self) -> None:
        database.add_verified_user("123456", "student456")
        database.add_verified_user("123456", "student789")
        
        # Verify the student ID was updated
        self.assertFalse(database.is_student_id_used("student456"))
        self.assertTrue(database.is_student_id_used("student789"))

    def test_add_strike(self) -> None:
        # Adding strike to new user
        database.add_strike("123456")
        state = database.get_user_state("123456")
        self.assertEqual(state, (1, False))

        # Adding second strike should lock the user
        database.add_strike("123456")
        state = database.get_user_state("123456")
        self.assertEqual(state, (2, True))

        # Adding third strike should keep it locked
        database.add_strike("123456")
        state = database.get_user_state("123456")
        self.assertEqual(state, (3, True))

    def test_unlock_user(self) -> None:
        database.add_strike("123456")
        database.add_strike("123456")
        state = database.get_user_state("123456")
        self.assertEqual(state, (2, True))

        # Unlock user
        database.unlock_user("123456")
        state = database.get_user_state("123456")
        self.assertEqual(state, (0, False))

    def test_reset_all_locks(self) -> None:
        database.add_strike("user1")
        database.add_strike("user1")
        database.add_strike("user2")
        database.add_strike("user2")
        self.assertEqual(database.get_user_state("user1"), (2, True))
        self.assertEqual(database.get_user_state("user2"), (2, True))

        database.reset_all_locks()
        self.assertEqual(database.get_user_state("user1"), (0, False))
        self.assertEqual(database.get_user_state("user2"), (0, False))

    def test_get_user_state_nonexistent(self) -> None:
        state = database.get_user_state("nonexistent")
        self.assertIsNone(state)

    def test_hash_student_id(self) -> None:
        # Test None returns None
        self.assertIsNone(database.hash_student_id(None))
        
        # Test hashing strips whitespace and hashes correctly
        raw_id = "  student123  "
        pepper = b"default_secret_pepper_for_testing_only_32_characters_long"
        expected_hash = hmac.new(pepper, b"student123", hashlib.sha256).hexdigest()
        self.assertEqual(database.hash_student_id(raw_id), expected_hash)

    def test_database_stores_hashed_student_id(self) -> None:
        raw_id = "student456"
        expected_hash = database.hash_student_id(raw_id)
        
        database.add_verified_user("123456", raw_id)
        
        # Query directly from database to verify cleartext is NOT stored
        with sqlite3.connect(database.DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT student_hash FROM verified_students WHERE discord_id = ?", (123456,))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            stored_student_id = row[0]
            
            # Verify that stored student_id is hashed and matches the expected hash
            self.assertEqual(stored_student_id, expected_hash)
            self.assertNotEqual(stored_student_id, raw_id)

    def test_get_student_by_id_and_discord_id(self) -> None:
        raw_id = "student999"
        database.add_verified_user("123456789", raw_id)

        # Test get_student_by_id with raw student_id
        res = database.get_student_by_id(raw_id)
        self.assertIsNotNone(res)
        self.assertEqual(res["discord_id"], 123456789)
        self.assertEqual(res["student_id_hash"], database.hash_student_id(raw_id))

        # Test get_student_by_discord_id with integer discord_id
        res2 = database.get_student_by_discord_id(123456789)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["discord_id"], 123456789)
        self.assertEqual(res2["student_id_hash"], database.hash_student_id(raw_id))

        # Test nonexistent
        self.assertIsNone(database.get_student_by_id("nonexistent"))
        self.assertIsNone(database.get_student_by_discord_id(999999999))

    def test_delete_student_record_and_by_discord_id(self) -> None:
        raw_id1 = "student111"
        raw_id2 = "student222"
        database.add_verified_user("111111", raw_id1)
        database.add_verified_user("222222", raw_id2)

        # Delete by student record
        deleted = database.delete_student_record(raw_id1)
        self.assertTrue(deleted)
        self.assertIsNone(database.get_student_by_id(raw_id1))

        # Delete by discord id
        deleted2 = database.delete_student_by_discord_id(222222)
        self.assertTrue(deleted2)
        self.assertIsNone(database.get_student_by_discord_id(222222))

        # Deleting non-existent returns False
        self.assertFalse(database.delete_student_record("nonexistent"))
        self.assertFalse(database.delete_student_by_discord_id(999999))

    def test_hmac_sha256_rainbow_table_resistance(self) -> None:
        raw_id = "123456789"
        pepper = "default_secret_pepper_for_testing_only_32_characters_long"
        os.environ["HMAC_SECRET_PEPPER"] = pepper
        
        expected = hmac.new(pepper.encode(), raw_id.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(database.hash_student_id(raw_id), expected)
        
        unkeyed_hash = hashlib.sha256(raw_id.encode()).hexdigest()
        self.assertNotEqual(database.hash_student_id(raw_id), unkeyed_hash)

    def test_sqlite_strict_table_typing(self) -> None:
        with database._connect() as conn:
            cursor = conn.cursor()
            with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)):
                cursor.execute(
                    "INSERT INTO verified_students (discord_id, student_hash, verified_at) VALUES (?, ?, ?)",
                    (b"\x00\x01\x02", "some_hash", "2025-01-01")
                )

if __name__ == "__main__":
    unittest.main()
