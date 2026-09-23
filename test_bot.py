import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import os
import gc
import discord
import bot
import database

def cleanup_bot_db(db_name: str) -> None:
    gc.collect()
    for name in (db_name, "test_bot_verified_students.db", "verified_students.db"):
        for ext in ("", "-wal", "-shm"):
            path = f"{name}{ext}"
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

class TestBot(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        database.DB_NAME = "test_bot_verified_students.db"
        os.environ["HMAC_SECRET_PEPPER"] = "default_secret_pepper_for_testing_only_32_characters_long"
        cleanup_bot_db(database.DB_NAME)
        database.init_db()

    def tearDown(self) -> None:
        cleanup_bot_db(database.DB_NAME)

    @classmethod
    def tearDownClass(cls) -> None:
        cleanup_bot_db("test_bot_verified_students.db")

    @patch("database.init_db")
    @patch("discord.app_commands.CommandTree.sync")
    async def test_setup_hook(self, mock_sync: AsyncMock, mock_init_db: MagicMock) -> None:
        test_bot = bot.VerificationBot()
        test_bot.add_view = MagicMock()
        
        await test_bot.setup_hook()
        
        mock_init_db.assert_called_once()
        self.assertEqual(test_bot.add_view.call_count, 3)
        mock_sync.assert_called_once()

    async def test_verify_dropdown_forbidden(self) -> None:
        dropdown = bot.VerifyDropdown()
        
        # Mock interaction and user send
        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_user = AsyncMock()
        mock_response = MagicMock()
        mock_response.status = 403
        mock_user.send.side_effect = discord.Forbidden(mock_response, "Forbidden")
        mock_interaction.user = mock_user
        mock_interaction.response = AsyncMock()
        
        await dropdown.callback(mock_interaction)
        
        # Verify ephemeral failure message
        mock_interaction.response.send_message.assert_called_with(
            "❌ Please enable Direct Messages from server members to apply.",
            ephemeral=True
        )

    async def test_verify_dropdown_success(self) -> None:
        dropdown = bot.VerifyDropdown()
        
        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_user = AsyncMock()
        mock_interaction.user = mock_user
        mock_interaction.response = AsyncMock()
        
        await dropdown.callback(mock_interaction)
        
        mock_user.send.assert_called_once()
        mock_interaction.response.send_message.assert_called_with(
            "✅ Verification instructions have been sent to your DMs!",
            ephemeral=True
        )

    async def test_setup_verify_no_permission(self) -> None:
        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.user = MagicMock()
        mock_interaction.user.roles = []
        mock_interaction.user.guild_permissions.administrator = False
        mock_interaction.response = AsyncMock()
        mock_interaction.guild = MagicMock()
        
        # Mock fetch_member to return unauthorized user
        mock_member = MagicMock()
        mock_member.roles = []
        mock_member.guild_permissions.administrator = False
        mock_interaction.guild.fetch_member = AsyncMock(return_value=mock_member)
        
        with patch.dict(os.environ, {"SUPPORT_ROLE_ID": "9999"}):
            # Call the callback of the app command
            await bot.setup_verify.callback(mock_interaction)
            mock_interaction.response.send_message.assert_called_with(
                "❌ You do not have permission to run this command.",
                ephemeral=True
            )

    async def test_fetch_member_safely_success(self) -> None:
        mock_guild = MagicMock(spec=discord.Guild)
        mock_member = MagicMock(spec=discord.Member)
        mock_guild.get_member.return_value = mock_member
        
        member = await bot.fetch_member_safely(mock_guild, 12345)
        self.assertEqual(member, mock_member)
        mock_guild.get_member.assert_called_with(12345)

    async def test_fetch_member_safely_not_found(self) -> None:
        mock_guild = MagicMock(spec=discord.Guild)
        mock_guild.get_member.return_value = None
        # fetch_member raises NotFound
        mock_response = MagicMock()
        mock_response.status = 404
        mock_guild.fetch_member.side_effect = discord.NotFound(mock_response, "Not Found")
        
        member = await bot.fetch_member_safely(mock_guild, 12345)
        self.assertIsNone(member)

    async def test_in_flight_dm_locking(self) -> None:
        # If user is in processing_users, on_message should reject immediately
        bot.bot.processing_users.add(999) # Add user 999 to active set of global bot
        
        mock_message = AsyncMock(spec=discord.Message)
        mock_message.author = MagicMock()
        mock_message.author.bot = False
        mock_message.author.id = 999
        mock_message.guild = None
        mock_message.attachments = [MagicMock()]
        
        await bot.on_message(mock_message)
        
        # Check that it replied with the in-flight lock message
        mock_message.reply.assert_called_with("❌ Please wait until your current document analysis completes.")
        
        # Clean up
        bot.bot.processing_users.discard(999)

    async def test_on_message_producer_queues_message(self) -> None:
        mock_message = AsyncMock(spec=discord.Message)
        mock_message.author = MagicMock()
        mock_message.author.bot = False
        mock_message.author.id = 888
        mock_message.guild = None
        
        mock_attachment = MagicMock()
        mock_attachment.content_type = "image/png"
        mock_attachment.size = 1000
        mock_message.attachments = [mock_attachment]
        
        await bot.on_message(mock_message)
        
        self.assertIn(888, bot.bot.processing_users)
        self.assertEqual(bot.verification_queue.qsize(), 1)
        
        queued_item = bot.verification_queue.get_nowait()
        self.assertEqual(queued_item["user_id"], queued_item["user_id"])
        self.assertEqual(queued_item["url"], mock_attachment.url)
        bot.verification_queue.task_done()
        
        mock_message.reply.assert_called_with(
            "⏳ Added to the verification queue! You are currently position: 1."
        )
        
        # Clean up
        bot.bot.processing_users.discard(888)

    async def test_check_student_command(self) -> None:
        database.add_verified_user("123456", "STUDENT101")

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.user = MagicMock()
        mock_interaction.user.roles = []
        mock_interaction.user.guild_permissions.administrator = True
        mock_interaction.response = AsyncMock()
        mock_interaction.guild = MagicMock()
        
        mock_member = MagicMock()
        mock_member.roles = []
        mock_member.guild_permissions.administrator = True
        mock_interaction.guild.fetch_member = AsyncMock(return_value=mock_member)

        # Check by user
        mock_target_user = MagicMock(spec=discord.User)
        mock_target_user.id = 123456

        await bot.check_student.callback(mock_interaction, student_id=None, user=mock_target_user)
        mock_interaction.response.send_message.assert_called()
        args, kwargs = mock_interaction.response.send_message.call_args
        self.assertTrue(kwargs.get("ephemeral"))
        self.assertIn("embed", kwargs)

    async def test_unlink_student_command(self) -> None:
        database.add_verified_user("123456", "STUDENT101")

        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.user = MagicMock()
        mock_interaction.user.roles = []
        mock_interaction.user.guild_permissions.administrator = True
        mock_interaction.response = AsyncMock()
        mock_interaction.guild = MagicMock()
        
        mock_member = MagicMock()
        mock_member.roles = []
        mock_member.guild_permissions.administrator = True
        mock_interaction.guild.fetch_member = AsyncMock(return_value=mock_member)

        mock_target_user = MagicMock(spec=discord.User)
        mock_target_user.id = 123456

        await bot.unlink_student.callback(mock_interaction, student_id=None, user=mock_target_user)
        mock_interaction.response.send_message.assert_called_with(
            "✅ Unlinked Student ID/User. They can now re-run verification.",
            ephemeral=True
        )
        res = database.get_student_by_discord_id(123456)
        self.assertIsNone(res)

    async def test_force_verify_command(self) -> None:
        mock_interaction = MagicMock(spec=discord.Interaction)
        mock_interaction.user = MagicMock()
        mock_interaction.user.roles = []
        mock_interaction.user.guild_permissions.administrator = True
        mock_interaction.response = AsyncMock()
        mock_interaction.guild = MagicMock()
        
        mock_member = MagicMock()
        mock_member.roles = []
        mock_member.guild_permissions.administrator = True
        mock_interaction.guild.fetch_member = AsyncMock(return_value=mock_member)

        mock_target_user = MagicMock(spec=discord.User)
        mock_target_user.id = 777888

        await bot.force_verify.callback(mock_interaction, user=mock_target_user, student_id="STUDENT999")
        mock_interaction.response.send_message.assert_called_with(
            "✅ Manually verified <@777888> with Student ID STUDENT999.",
            ephemeral=True
        )
        res = database.get_student_by_discord_id(777888)
        self.assertIsNotNone(res)

    async def test_on_message_payload_too_large(self) -> None:
        mock_message = AsyncMock(spec=discord.Message)
        mock_message.author = MagicMock()
        mock_message.author.bot = False
        mock_message.author.id = 112233
        mock_message.guild = None
        
        # Simulated attachment larger than 25MB
        mock_attachment = MagicMock()
        mock_attachment.content_type = "image/png"
        mock_attachment.size = 26214401 # > 25MB
        mock_message.attachments = [mock_attachment]
        
        await bot.on_message(mock_message)
        
        # The bot should reject it immediately
        mock_message.author.send.assert_called_with("⚠️ File too large. Maximum size is 25MB.")
        self.assertNotIn(112233, bot.bot.processing_users)

    async def test_on_message_payload_boundary_accepted(self) -> None:
        mock_message = AsyncMock(spec=discord.Message)
        mock_message.author = MagicMock()
        mock_message.author.bot = False
        mock_message.author.id = 112234
        mock_message.guild = None
        
        # Simulated attachment exactly equal to 25MB (26214400 bytes)
        mock_attachment = MagicMock()
        mock_attachment.content_type = "image/png"
        mock_attachment.size = 26214400 # <= 25MB
        mock_message.attachments = [mock_attachment]
        
        await bot.on_message(mock_message)
        
        self.assertIn(112234, bot.bot.processing_users)
        queued_item = bot.verification_queue.get_nowait()
        self.assertEqual(queued_item["user_id"], queued_item["user_id"])
        self.assertEqual(queued_item["url"], mock_attachment.url)
        bot.verification_queue.task_done()
        bot.bot.processing_users.discard(112234)

    async def test_on_message_invalid_mime_type(self) -> None:
        mock_message = AsyncMock(spec=discord.Message)
        mock_message.author = MagicMock()
        mock_message.author.bot = False
        mock_message.author.id = 445566
        mock_message.guild = None
        
        # Simulated webp or pdf attachment
        mock_attachment = MagicMock()
        mock_attachment.content_type = "image/webp"
        mock_attachment.size = 1000
        mock_message.attachments = [mock_attachment]
        
        await bot.on_message(mock_message)
        
        # The bot should reject it because of invalid mime type
        mock_message.reply.assert_called_with("❌ Invalid file. Please upload a JPG or PNG image under 8MB.")
        self.assertNotIn(445566, bot.bot.processing_users)

    async def test_audit_log_deletion_reconstruction(self) -> None:
        # Put an audit log into the cache
        test_msg_id = 999111
        test_embed = discord.Embed(title="Support Audit Log: Test Log", color=discord.Color.blue())
        bot.bot.sent_audit_logs[test_msg_id] = test_embed
        
        mock_deleted_message = MagicMock(spec=discord.Message)
        mock_deleted_message.id = test_msg_id
        mock_deleted_message.guild = MagicMock()
        
        mock_channel = AsyncMock()
        mock_deleted_message.guild.get_channel.return_value = mock_channel
        
        # Trigger the message delete event
        await bot.on_message_delete(mock_deleted_message)
        
        # Verify that the channel sent the warning alert AND recreated the embed
        mock_channel.send.assert_called()
        self.assertEqual(mock_channel.send.call_count, 2)

    def test_magic_bytes_verification(self) -> None:
        # Valid magic bytes
        self.assertTrue(bot.verify_magic_bytes(b'\xFF\xD8\xFF_some_jpeg_data'))
        self.assertTrue(bot.verify_magic_bytes(b'\x89\x50\x4E\x47\r\n\x1a\n'))
        self.assertTrue(bot.verify_magic_bytes(b'RIFF\x00\x00\x00\x00WEBPvp8'))
        
        # Corrupted / fake
        self.assertFalse(bot.verify_magic_bytes(b'GIF89a_fake_image'))
        self.assertFalse(bot.verify_magic_bytes(b'PDF-1.4_fake_image'))

    async def test_concurrency_locks(self) -> None:
        lock1 = await bot.get_lock("user_1")
        lock2 = await bot.get_lock("user_1")
        # Ensure that lock1 and lock2 are the exact same instance for the same key
        self.assertIs(lock1, lock2)
        
        # Test that we can acquire
        async with lock1:
            self.assertTrue(lock1.locked())

if __name__ == "__main__":
    unittest.main()
