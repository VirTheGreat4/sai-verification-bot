import unittest
from unittest.mock import patch, MagicMock
import os
import io
import gc
from PIL import Image
import ai_engine

def cleanup_ai_db() -> None:
    gc.collect()
    for name in ("verified_students", "test_verified_students"):
        for ext in ("", "-wal", "-shm"):
            path = f"{name}.db{ext}"
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

class TestAIEngine(unittest.TestCase):
    def setUp(self) -> None:
        cleanup_ai_db()

    def tearDown(self) -> None:
        cleanup_ai_db()

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup_ai_db()

    def test_load_env_keys(self) -> None:
        # Create a temporary .env file for testing
        test_env_path = "test_temp.env"
        with open(test_env_path, "w", encoding="utf-8") as f:
            f.write("GEMINI_API_KEYS=mock_key1,mock_key2,mock_key3\n")
            
        try:
            keys = ai_engine.load_env_keys(test_env_path)
            self.assertEqual(keys, ["mock_key1", "mock_key2", "mock_key3"])
        finally:
            if os.path.exists(test_env_path):
                os.remove(test_env_path)

    def test_key_rotation(self) -> None:
        # Patch the key pool in ai_engine
        import itertools
        ai_engine.api_keys = ["k1", "k2"]
        ai_engine.key_pool = itertools.cycle(["k1", "k2"])
        
        key1 = ai_engine.rotate_api_key()
        self.assertEqual(key1, "k1")
        
        key2 = ai_engine.rotate_api_key()
        self.assertEqual(key2, "k2")
        
        key3 = ai_engine.rotate_api_key()
        self.assertEqual(key3, "k1")

    def test_load_reference_images_empty(self) -> None:
        # folder_path does not exist
        images = ai_engine.load_reference_images("nonexistent_folder_abc")
        self.assertEqual(images, [])

    @patch("google.genai.Client")
    def test_get_active_flash_models(self, mock_client_cls: MagicMock) -> None:
        # Mock returned models
        m1 = MagicMock()
        m1.name = "gemini-2.5-flash"
        m1.supported_actions = ["generateContent"]
        
        m2 = MagicMock()
        m2.name = "gemini-2.5-pro"
        m2.supported_actions = ["generateContent"]
        
        m3 = MagicMock()
        m3.name = "gemini-2.0-flash-latest"
        m3.supported_actions = ["generateContent"]
        
        mock_client = MagicMock()
        mock_client.models.list.return_value = [m1, m2, m3]
        mock_client_cls.return_value = mock_client
        
        models = ai_engine.get_active_flash_models()
        self.assertIn("gemini-2.5-flash", models)
        self.assertIn("gemini-2.0-flash-latest", models)
        self.assertNotIn("gemini-2.5-pro", models)
        self.assertEqual(models, ["gemini-2.5-flash", "gemini-2.0-flash-latest"])

    @patch("ai_engine.load_reference_images")
    @patch("ai_engine.optimize_image")
    @patch("google.genai.Client")
    @patch("ai_engine.get_active_flash_models")
    def test_verify_document_success(self, mock_get_models: MagicMock, mock_client_cls: MagicMock, mock_optimize: MagicMock, mock_load_refs: MagicMock) -> None:
        mock_get_models.return_value = ["gemini-2.5-flash"]
        mock_load_refs.return_value = []
        mock_optimize.return_value = b"optimized_bytes"
        
        # Mock model response conforming to new Pydantic target schema
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"status": "PASS", "reason": "NONE", "extracted_data": {"student_name": "Test Student", "student_number": "123456789", "program_year_level": "BSCS 3rd Year", "school_year_term": "2025-2026 1st Sem", "ms_office_email": "test.123456789@students.sti.edu"}}'
        mock_client.models.generate_content.return_value = mock_response
        mock_client_cls.return_value = mock_client
        
        # Create a dummy 1x1 black pixel image bytes
        img = Image.new("RGB", (1, 1), color="black")
        img_bytes_io = io.BytesIO()
        img.save(img_bytes_io, format="PNG")
        img_bytes = img_bytes_io.getvalue()
        
        result = ai_engine.verify_document(img_bytes)
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["reason"], "NONE")
        self.assertEqual(result["extracted_id"], "123456789")

    @patch("ai_engine.load_reference_images")
    @patch("ai_engine.optimize_image")
    @patch("google.genai.Client")
    @patch("ai_engine.get_active_flash_models")
    @patch("ai_engine.rotate_api_key")
    def test_verify_document_retry_on_429(self, mock_rotate_key: MagicMock, mock_get_models: MagicMock, mock_client_cls: MagicMock, mock_optimize: MagicMock, mock_load_refs: MagicMock) -> None:
        mock_get_models.return_value = ["gemini-2.5-flash"]
        mock_rotate_key.return_value = "rotated_key"
        mock_load_refs.return_value = []
        mock_optimize.return_value = b"optimized_bytes"
        
        mock_client = MagicMock()
        
        from google.genai import errors
        ex_429 = errors.APIError(429, "RESOURCE_EXHAUSTED", "Rate limit exceeded")
        
        mock_response = MagicMock()
        mock_response.text = '{"status": "FAIL", "reason": "INVALID_DOCUMENT", "extracted_data": {"student_name": null, "student_number": null, "program_year_level": null, "school_year_term": null, "ms_office_email": null}}'
        
        mock_client.models.generate_content.side_effect = [ex_429, mock_response]
        mock_client_cls.return_value = mock_client
        
        img = Image.new("RGB", (1, 1), color="black")
        img_bytes_io = io.BytesIO()
        img.save(img_bytes_io, format="PNG")
        img_bytes = img_bytes_io.getvalue()
        
        result = ai_engine.verify_document(img_bytes)
        
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["reason"], "INVALID_DOCUMENT")

    def test_optimize_image_success(self) -> None:
        # Create a dummy 100x100 RGBA image
        img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
        img_bytes_io = io.BytesIO()
        img.save(img_bytes_io, format="PNG")
        raw_bytes = img_bytes_io.getvalue()
        
        optimized = ai_engine.optimize_image(raw_bytes)
        self.assertIsNotNone(optimized)
        
        # Verify optimized image can be opened and is of RGB mode
        opt_img = Image.open(io.BytesIO(optimized))
        self.assertEqual(opt_img.mode, "RGB")
        self.assertTrue(opt_img.width <= 2000)
        self.assertTrue(opt_img.height <= 2000)

    def test_optimize_image_invalid(self) -> None:
        # Passing garbage bytes should return None
        optimized = ai_engine.optimize_image(b"not_an_image_garbage_bytes_xyz")
        self.assertIsNone(optimized)

    def test_sanitize_payload_string(self) -> None:
        # Input with null byte, control characters, Bidi override character, and zero-width space
        malicious_input = "Hello\x00World\x1f!\u202Ereversed\u200Btext"
        sanitized = ai_engine.sanitize_payload_string(malicious_input)
        self.assertEqual(sanitized, "HelloWorld!reversedtext")

    def test_sanitize_extracted_field(self) -> None:
        # Input with zero-width characters and control codes
        field_input = "\u200B\uFEFF12345\x006789\u200D"
        sanitized = ai_engine.sanitize_extracted_field(field_input)
        self.assertEqual(sanitized, "123456789")

    @patch("ai_engine.load_reference_images")
    @patch("ai_engine.optimize_image")
    @patch("google.genai.Client")
    @patch("ai_engine.get_active_flash_models")
    def test_verify_document_prompt_injection_suspected_tampering(self, mock_get_models: MagicMock, mock_client_cls: MagicMock, mock_optimize: MagicMock, mock_load_refs: MagicMock) -> None:
        mock_get_models.return_value = ["gemini-2.5-flash"]
        mock_load_refs.return_value = []
        mock_optimize.return_value = b"optimized_bytes"
        
        # Mock model response for a prompt injection jailbreak attempt where the model returned SUSPECTED_TAMPERING
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = '{"status": "FAIL", "reason": "SUSPECTED_TAMPERING", "extracted_data": {"student_name": null, "student_number": null, "program_year_level": null, "school_year_term": null, "ms_office_email": null}}'
        mock_client.models.generate_content.return_value = mock_response
        mock_client_cls.return_value = mock_client
        
        img = Image.new("RGB", (1, 1), color="black")
        img_bytes_io = io.BytesIO()
        img.save(img_bytes_io, format="PNG")
        img_bytes = img_bytes_io.getvalue()
        
        result = ai_engine.verify_document(img_bytes)
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["reason"], "SUSPECTED_TAMPERING")

    def test_decompression_bomb_handling(self) -> None:
        with patch("PIL.Image.open", side_effect=Image.DecompressionBombError("Decompression bomb")):
            res = ai_engine.verify_document(b"fake_bytes")
            self.assertFalse(res["verified"])
            self.assertEqual(res["reason"], "DECOMPRESSION_BOMB")

    def test_decompression_bomb_exceeds_max_pixels(self) -> None:
        # 12000 x 12000 = 144,000,000 pixels (> 2 * 67,108,864 = 134,217,728) or DecompressionBombWarning
        large_img = Image.new("RGB", (12000, 12000), color="white")
        buf = io.BytesIO()
        large_img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        with self.assertRaises((Image.DecompressionBombError, Image.DecompressionBombWarning)):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                ai_engine.optimize_image(raw_bytes)

    def test_realistic_camera_resolution_passes_decompression_check(self) -> None:
        # 4032 x 3024 = 12,192,768 pixels (realistic 12MP camera photo)
        cam_img = Image.new("RGB", (4032, 3024), color="blue")
        buf = io.BytesIO()
        cam_img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        optimized = ai_engine.optimize_image(raw_bytes)
        self.assertIsNotNone(optimized)

if __name__ == "__main__":
    unittest.main()
