import sys
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
import traceback
import os
import io
import sqlite3
import asyncio
import time
import discord
import hashlib
from PIL import Image

# Enforce an absolute 64 Megapixel ceiling to prevent decompression bombs (DoS)
Image.MAX_IMAGE_PIXELS = 67108864

from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from typing import Optional, Set, Union, Dict, Any

import database
import ai_engine

# Load .env variables from potential locations
load_dotenv(".env")
load_dotenv("reference_images/.env")

# Production Constants - Hardened: Max size is 8MB (8388608 bytes)
MAX_FILE_SIZE = 26214400  # 25MB (25 * 1024 * 1024 bytes)

# Module level queue for backward compatibility with existing tests
verification_queue = asyncio.Queue()

# Thread-safe global button/interaction cooldown tracker (1 request per user per 60 seconds)
BUTTON_COOLDOWNS = {}

def check_button_cooldown(user_id: int) -> Optional[float]:
    """
    Tracks and enforces interaction cooldowns (60 seconds) for button/select interactions.
    """
    current_time = time.time()
    last_time = BUTTON_COOLDOWNS.get(user_id, 0)
    if current_time - last_time < 60.0:
        return 60.0 - (current_time - last_time)
    BUTTON_COOLDOWNS[user_id] = current_time
    return None

def verify_magic_bytes(data: bytes) -> bool:
    """
    Verifies magic byte signatures for JPEG, PNG, or WebP.
    """
    if len(data) < 3:
        return False
    # JPEG check
    if data.startswith(b'\xFF\xD8\xFF'):
        return True
    # PNG check
    if data.startswith(b'\x89\x50\x4E\x47'):
        return True
    # WebP check (RIFF....WEBP)
    if len(data) >= 12 and data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return True
    return False

# In-memory TOCTOU prevention locks
LOCKS = {}
LOCKS_MUTEX = asyncio.Lock()

async def get_lock(key: Any) -> asyncio.Lock:
    async with LOCKS_MUTEX:
        if key not in LOCKS:
            LOCKS[key] = asyncio.Lock()
        return LOCKS[key]

class VerificationBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        # In-memory set for tracking active in-flight verification requests
        self.processing_users: Set[int] = set()
        # Aliasing instance-level verification_queue to the global verification_queue
        self.verification_queue = verification_queue
        # Cache of audit log message IDs and their original embeds to detect tampering
        self.sent_audit_logs: Dict[int, discord.Embed] = {}

    async def setup_hook(self) -> None:
        # Initialize database
        database.init_db()
        
        # Register persistent views
        self.add_view(VerifyDropdownView())
        self.add_view(DMStartCancelView())
        self.add_view(StaffButtonsView())
        
        # Sync slash commands
        await self.tree.sync()
        print("Bot persistent views registered and slash commands synced.")
        
        # Start the background worker
        try:
            self.loop.create_task(self.verification_worker())
        except AttributeError:
            asyncio.create_task(self.verification_worker())

    async def verification_worker(self) -> None:
        """
        Background worker strictly adhering to the requested try...except...finally structure.
        Ensures all synchronous blocking calls are run via asyncio.to_thread().
        """
        while True:
            message = await self.verification_queue.get()
            user_id = message.author.id
            user_id_str = str(user_id)
            print(f"[WORKER] Picked up verification job for user {user_id}. Starting analysis...", flush=True)
            try:
                # We already validated the attachment in on_message, so we know message.attachments[0] exists
                attachment = message.attachments[0]
                
                # Status message to indicate analysis has begun
                status_msg = await message.reply("⏳ Analyzing your SAI document... Please note: Verification may take a few moments while we audit your document. Thank you for your patience!")
                
                # Read attachment bytes (run async)
                try:
                    raw_bytes = await attachment.read()
                except Exception as e:
                    print(f"Error reading attachment: {e}")
                    await status_msg.edit(content="❌ Error: Failed to read file.")
                    continue

                # Magic Bytes File Signature Verification: Do NOT trust Discord's attachment.content_type
                if not verify_magic_bytes(raw_bytes):
                    await status_msg.edit(content="❌ Invalid file. The uploaded image does not have a valid file signature (JPEG, PNG, or WebP).")
                    continue

                # Optimize image and catch PIL Decompression Bomb Protection
                try:
                    optimized_bytes = await asyncio.to_thread(ai_engine.optimize_image, raw_bytes)
                except Image.DecompressionBombError as dbe:
                    print(f"Decompression bomb detected: {dbe}")
                    await status_msg.edit(content="❌ Image processing aborted: Decompression bomb detected (exceeded 8 Megapixel ceiling).")
                    continue
                except Exception as e:
                    print(f"Error reading attachment: {e}")
                    optimized_bytes = None

                if optimized_bytes is None:
                    await status_msg.edit(content="❌ Invalid file. The uploaded image is corrupted or invalid.")
                    continue

                # Atomic TOCTOU Prevention: lock per user
                user_lock = await get_lock(user_id)
                async with user_lock:
                    # Offload synchronous AI engine call to thread pool via asyncio.to_thread
                    result = await asyncio.to_thread(ai_engine.verify_document, optimized_bytes)

                    verified = result.get("verified", False)
                    reason = result.get("reason", "Verification unsuccessful.")
                    extracted_id = result.get("extracted_id", "").strip()

                    if reason == "DECOMPRESSION_BOMB":
                        await status_msg.edit(content="❌ Image processing aborted: Decompression bomb detected (exceeded 8 Megapixel ceiling).")
                        continue

                    # PASS Logic
                    if verified:
                        student_hash = await asyncio.to_thread(database.hash_student_id, extracted_id)
                        # Atomic TOCTOU Prevention: lock per student hash
                        hash_lock = await get_lock(student_hash)
                        async with hash_lock:
                            is_used = await asyncio.to_thread(database.is_student_id_used, extracted_id)
                            if is_used:
                                # Treat as duplicate -> Fail
                                verified = False
                                reason = f"Duplicate verification: Student ID '{extracted_id}' is already registered to another user."

                                # Send audit alert to MOD_LOG_CHANNEL_ID
                                existing_record = await asyncio.to_thread(database.get_student_by_id, extracted_id)
                                original_discord_id = existing_record["discord_id"] if existing_record else "Unknown"
                                mod_log_id = int(os.environ.get("MOD_LOG_CHANNEL_ID", 0))
                                if mod_log_id and message.guild:
                                    try:
                                        mod_channel = message.guild.get_channel(mod_log_id)
                                        if not mod_channel:
                                            mod_channel = await message.guild.fetch_channel(mod_log_id)
                                        if mod_channel:
                                            mod_embed = discord.Embed(
                                                title="⚠️ Duplicate Student ID Detected",
                                                description="A student submitted a Student ID that is already registered in the system.",
                                                color=discord.Color.gold()
                                            )
                                            mod_embed.add_field(name="Applicant", value=f"<@{user_id}>", inline=True)
                                            mod_embed.add_field(name="Existing Registered User", value=f"<@{original_discord_id}>", inline=True)
                                            mod_embed.add_field(
                                                name="Action Needed",
                                                value="Staff review required via `/check-student` or `/unlink-student`.",
                                                inline=False
                                            )
                                            msg = await mod_channel.send(embed=mod_embed)
                                            self.sent_audit_logs[msg.id] = mod_embed
                                    except Exception as e:
                                        print(f"Failed to send mod log embed: {e}")
                            else:
                                await asyncio.to_thread(database.add_verified_user, user_id_str, extracted_id)
                                
                                guild_id = int(os.environ.get("GUILD_ID", 0))
                                guild = message.guild or self.get_guild(guild_id)
                                role_assigned = False
                                if guild:
                                    role = guild.get_role(int(os.environ.get("VERIFIED_ROLE_ID", 0)))
                                    # DM Member Fetch Safety: safely fetch member
                                    member = await fetch_member_safely(guild, user_id)
                                    if member and role:
                                        try:
                                            await member.add_roles(role)
                                            role_assigned = True
                                        except Exception as e:
                                            print(f"Failed to assign role to {user_id_str} on success: {e}")

                                success_embed = discord.Embed(
                                    title="Verification Successful",
                                    description="🎉 Your student student verification has been approved automatically!",
                                    color=discord.Color.green()
                                )
                                success_embed.add_field(name="Student ID", value=extracted_id, inline=True)
                                if role_assigned:
                                    success_embed.add_field(name="Role Assigned", value="Verified Student", inline=True)
                                else:
                                    success_embed.add_field(name="Role Status", value="Role pending assignment.", inline=True)
                                    
                                await status_msg.edit(content="✅ Analysis complete!")
                                await message.reply(embed=success_embed)
                                continue

                    # FAIL Logic (Warning or Lock)
                    await asyncio.to_thread(database.add_strike, user_id_str)
                    updated_state = await asyncio.to_thread(database.get_user_state, user_id_str)
                    strikes = 1
                    is_locked = False
                    if updated_state:
                        strikes, is_locked = updated_state

                    if is_locked or strikes >= 2:
                        # Strike 2 (Lock user & forward to staff)
                        await status_msg.edit(content="❌ Verification failed.")
                        
                        lock_embed = discord.Embed(
                            title="Verification Locked",
                            description=(
                                "❌ You have accumulated 2 strikes. Your verification has been locked "
                                "and forwarded to server staff for manual review. Please wait for assistance."
                            ),
                            color=discord.Color.red()
                        )
                        lock_embed.add_field(name="Failure Reason", value=reason, inline=False)
                        await message.reply(embed=lock_embed)
                        
                        # Send to staff pending channel
                        pending_channel_id = int(os.environ.get("PENDING_CHANNEL_ID", 0))
                        guild_id = int(os.environ.get("GUILD_ID", 0))
                        guild = message.guild or self.get_guild(guild_id)
                        pending_channel = None
                        if guild:
                            pending_channel = guild.get_channel(pending_channel_id)
                            if not pending_channel:
                                try:
                                    pending_channel = await guild.fetch_channel(pending_channel_id)
                                except Exception:
                                    pass
                        if not pending_channel:
                            pending_channel = self.get_channel(pending_channel_id)

                        if pending_channel:
                            view = StaffButtonsView()
                            staff_embed = discord.Embed(
                                title="Manual Verification Required",
                                description="User has accumulated 2 strikes and is locked. Please review the attached document.",
                                color=discord.Color.orange()
                            )
                            staff_embed.add_field(name="User Mention", value=message.author.mention, inline=True)
                            staff_embed.add_field(name="User ID", value=user_id_str, inline=True)
                            staff_embed.add_field(name="Student ID", value=extracted_id if extracted_id else "N/A", inline=True)
                            staff_embed.add_field(name="Failure Reason", value=reason, inline=False)

                            # Re-upload the optimized image buffer directly (always < 10MB, no URLs)
                            try:
                                file_to_forward = await asyncio.to_thread(
                                    lambda: discord.File(io.BytesIO(optimized_bytes), filename="sai_document.jpg")
                                )
                                await pending_channel.send(embed=staff_embed, file=file_to_forward, view=view)
                            except Exception as e:
                                print(f"Failed to forward verification image to staff: {e}")
                                await pending_channel.send(embed=staff_embed, view=view)
                    else:
                        # Strike 1: Warning
                        await status_msg.edit(content="❌ Verification failed.")
                        
                        warn_embed = discord.Embed(
                            title="Verification Warning (Strike 1/2)",
                            description=(
                                "⚠️ Your document verification failed. You have received 1 strike. "
                                "You have one remaining attempt before your account is locked and sent to manual review."
                            ),
                            color=discord.Color.yellow()
                        )
                        warn_embed.add_field(name="Failure Reason", value=reason, inline=False)
                        await message.reply(embed=warn_embed)

            except Exception as e:
                # Log error and notify user via DM
                print(f"Error in verification worker processing: {e}", flush=True)
                traceback.print_exc(file=sys.stdout)
                sys.stdout.flush()
                try:
                    await message.author.send("❌ An error occurred while analyzing your document. Please try again in a few moments.")
                except Exception:
                    try:
                        await message.reply("❌ An error occurred while analyzing your document. Please try again in a few moments.")
                    except Exception:
                        pass
            finally:
                self.verification_queue.task_done()
                self.processing_users.discard(user_id)


# ==================== HELPER FUNCTIONS ====================

async def fetch_member_safely(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    """
    Safely retrieves a member from the guild.
    Tries memory cache first, then API fetch.
    Handles discord.NotFound and discord.HTTPException.
    """
    try:
        member = guild.get_member(user_id)
        if not member:
            member = await guild.fetch_member(user_id)
        return member
    except (discord.NotFound, discord.HTTPException) as e:
        print(f"Failed to fetch member {user_id} safely: {e}")
        return None

async def send_audit_log(guild: Optional[discord.Guild], embed: discord.Embed) -> None:
    """
    Sends support / administrative audit log embeds to the configured MOD_LOG_CHANNEL_ID.
    Saves the message ID and its embed to prevent moderator tampering.
    """
    if not guild:
        return
    mod_log_id = int(os.environ.get("MOD_LOG_CHANNEL_ID", 0))
    if not mod_log_id:
        return
    try:
        mod_channel = guild.get_channel(mod_log_id)
        if not mod_channel:
            mod_channel = await guild.fetch_channel(mod_log_id)
        if mod_channel:
            msg = await mod_channel.send(embed=embed)
            bot.sent_audit_logs[msg.id] = embed
    except Exception as e:
        print(f"Failed to send mod log: {e}")


# ==================== PERSISTENT VIEWS ====================

class VerifyDropdown(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label="Apply for Verification",
                description="Start the student verification process.",
                emoji="🎓",
                value="apply_verification"
            )
        ]
        super().__init__(
            placeholder="Select to verify your student status...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="persistent:verify_dropdown_select"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        user = interaction.user
        
        # Enforce button interaction cooldown
        cooldown_left = check_button_cooldown(user.id)
        if cooldown_left is not None:
            await interaction.response.send_message(
                f"⚠️ Please wait {cooldown_left:.1f} seconds before requesting verification again.",
                ephemeral=True
            )
            return

        try:
            view = DMStartCancelView()
            embed = discord.Embed(
                title="Student Verification",
                description=(
                    "Welcome to student verification! Please click **Start Verification** to begin. "
                    "You will then be prompted to upload an image of your **Student Assessment Invoice**."
                ),
                color=discord.Color.blue()
            )
            await user.send(embed=embed, view=view)
            await interaction.response.send_message(
                "✅ Verification instructions have been sent to your DMs!",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Please enable Direct Messages from server members to apply.",
                ephemeral=True
            )

class VerifyDropdownView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(VerifyDropdown())

class DMStartCancelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start Verification",
        style=discord.ButtonStyle.success,
        custom_id="persistent:dm_start_btn"
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Enforce button interaction cooldown
        cooldown_left = check_button_cooldown(interaction.user.id)
        if cooldown_left is not None:
            await interaction.response.send_message(
                f"⚠️ Please wait {cooldown_left:.1f} seconds.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Upload Document",
            description=(
                "Please upload your **Student Assessment Invoice** (as a JPG or PNG image under 8MB) "
                "directly in this DM channel."
            ),
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        custom_id="persistent:dm_cancel_btn"
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Enforce button interaction cooldown
        cooldown_left = check_button_cooldown(interaction.user.id)
        if cooldown_left is not None:
            await interaction.response.send_message(
                f"⚠️ Please wait {cooldown_left:.1f} seconds.",
                ephemeral=True
            )
            return
            
        await interaction.response.send_message("❌ Verification cancelled. You can restart anytime using the dropdown.")

class StaffButtonsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        custom_id="persistent:staff_accept_btn"
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Error: Must be used in a server.", ephemeral=True)
            return
            
        # Privilege Verification (Anti-Spoofing): Bypass cached interaction.user.roles
        try:
            member = await guild.fetch_member(interaction.user.id)
        except Exception:
            await interaction.response.send_message("❌ Error: Member not found.", ephemeral=True)
            return
            
        support_role_id = int(os.environ.get("SUPPORT_ROLE_ID", 0))
        is_authorized = any(role.id == support_role_id for role in member.roles) or member.guild_permissions.administrator
        if not is_authorized:
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return
            
        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message("❌ Error: Embed message not found.", ephemeral=True)
            return
            
        embed = interaction.message.embeds[0]
        user_id = None
        student_id = None
        for field in embed.fields:
            if field.name == "User ID":
                user_id = int(field.value)
            elif field.name == "Student ID":
                student_id = field.value
                
        if not user_id:
            await interaction.response.send_message("❌ Error: Could not parse User ID.", ephemeral=True)
            return
            
        # Atomic TOCTOU Prevention: lock per student hash during manual approval
        student_hash = await asyncio.to_thread(database.hash_student_id, student_id)
        hash_lock = await get_lock(student_hash)
        async with hash_lock:
            is_used_by_other = False
            if student_id and student_id != "N/A" and student_id.strip():
                is_used_by_other = await asyncio.to_thread(
                    database.is_student_id_used_by_other, student_id, str(user_id)
                )
                        
            if is_used_by_other:
                # Disable buttons to prevent further actions
                for child in self.children:
                    child.disabled = True
                    
                new_embed = discord.Embed.from_dict(embed.to_dict())
                new_embed.color = discord.Color.red()
                new_embed.add_field(
                    name="❌ Verification Blocked",
                    value=f"Duplicate Student ID detected: Student ID '{student_id}' is already registered to another Discord user. Approval blocked.",
                    inline=False
                )
                await interaction.response.edit_message(embed=new_embed, view=self)
                return

            # Action: Unlock user & verify them (offloaded)
            await asyncio.to_thread(database.unlock_user, str(user_id))
            if student_id and student_id != "N/A" and student_id.strip():
                await asyncio.to_thread(database.add_verified_user, str(user_id), student_id)
            else:
                await asyncio.to_thread(database.add_verified_user, str(user_id), f"STAFF_VERIFIED_{user_id}")
                
        # DM Member Fetch Safety: safely retrieve member
        target_member = await fetch_member_safely(guild, user_id)
                
        verified_role_id = int(os.environ.get("VERIFIED_ROLE_ID", 0))
        role_assigned = False
        if target_member:
            role = guild.get_role(verified_role_id)
            if role:
                try:
                    await target_member.add_roles(role)
                    role_assigned = True
                except Exception as e:
                    print(f"Failed to assign role to {user_id}: {e}")
                    
        # Notify user
        if target_member:
            try:
                success_embed = discord.Embed(
                    title="Verification Approved",
                    description="🎉 Congratulations! Your student verification has been approved by staff.",
                    color=discord.Color.green()
                )
                await target_member.send(embed=success_embed)
            except Exception as e:
                print(f"Failed to DM approved user {user_id}: {e}")
                
        # Disable buttons
        for child in self.children:
            child.disabled = True
            
        new_embed = discord.Embed.from_dict(embed.to_dict())
        new_embed.color = discord.Color.green()
        new_embed.add_field(name="Status", value=f"✅ Approved by {member.mention}", inline=False)
        await interaction.response.edit_message(embed=new_embed, view=self)

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        custom_id="persistent:staff_deny_btn"
    )
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Error: Must be used in a server.", ephemeral=True)
            return
            
        # Privilege Verification (Anti-Spoofing): Bypass cached interaction.user.roles
        try:
            member = await guild.fetch_member(interaction.user.id)
        except Exception:
            await interaction.response.send_message("❌ Error: Member not found.", ephemeral=True)
            return
            
        support_role_id = int(os.environ.get("SUPPORT_ROLE_ID", 0))
        is_authorized = any(role.id == support_role_id for role in member.roles) or member.guild_permissions.administrator
        if not is_authorized:
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return
            
        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message("❌ Error: Embed message not found.", ephemeral=True)
            return
            
        embed = interaction.message.embeds[0]
        user_id = None
        for field in embed.fields:
            if field.name == "User ID":
                user_id = int(field.value)
                
        if not user_id:
            await interaction.response.send_message("❌ Error: Could not parse User ID.", ephemeral=True)
            return
            
        # Action: Unlock user so they can try again, but do not verify (offloaded)
        await asyncio.to_thread(database.unlock_user, str(user_id))
        
        # DM Member Fetch Safety: safely retrieve member
        target_member = await fetch_member_safely(guild, user_id)
                
        if target_member:
            try:
                deny_embed = discord.Embed(
                    title="Verification Denied",
                    description="❌ Your student student verification request was denied by staff. You may try re-submitting your document.",
                    color=discord.Color.red()
                )
                await target_member.send(embed=deny_embed)
            except Exception as e:
                print(f"Failed to DM denied user {user_id}: {e}")
                
        # Disable buttons
        for child in self.children:
            child.disabled = True
            
        new_embed = discord.Embed.from_dict(embed.to_dict())
        new_embed.color = discord.Color.red()
        new_embed.add_field(name="Status", value=f"❌ Denied by {member.mention}", inline=False)
        await interaction.response.edit_message(embed=new_embed, view=self)


bot = VerificationBot()

# ==================== BOT SLASH COMMANDS ====================

async def is_staff_or_admin(interaction: discord.Interaction) -> bool:
    """
    Checks if the interacting user has the configured Support role or is an Admin.
    Bypasses cached roles, explicitly fetching the member from Discord API.
    """
    if not interaction.guild:
        return False
    support_role_id = int(os.environ.get("SUPPORT_ROLE_ID", 0))
    try:
        member = await interaction.guild.fetch_member(interaction.user.id)
    except Exception:
        return False
        
    if any(role.id == support_role_id for role in member.roles):
        return True
    if member.guild_permissions.administrator:
        return True
    return False


@bot.tree.command(name="setup-verify", description="Sets up the student verification channel with the dropdown menu.")
@app_commands.checks.cooldown(1, 60.0, key=lambda i: (i.guild_id or 0, i.user.id))
async def setup_verify(interaction: discord.Interaction) -> None:
    # Restrict to Support Role or Admin
    if not await is_staff_or_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission to run this command.", ephemeral=True)
        return

    verify_channel_id = int(os.environ.get("VERIFY_CHANNEL_ID", 0))
    channel = bot.get_channel(verify_channel_id)
    if not channel:
        await interaction.response.send_message(
            f"❌ Verification channel with ID {verify_channel_id} not found in cache. Make sure the bot has access.",
            ephemeral=True
        )
        return
        
    embed = discord.Embed(
        title="Student Verification Required",
        description="To access the channels on this server, please verify your student status by choosing an option below.",
        color=discord.Color.blue()
    )
    view = VerifyDropdownView()
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Verification dropdown posted successfully!", ephemeral=True)


@bot.tree.command(name="check-student", description="Inspect a student's verification status by Student ID or User.")
@app_commands.describe(student_id="Raw Student ID", user="Discord user to check")
@app_commands.checks.cooldown(1, 60.0, key=lambda i: (i.guild_id or 0, i.user.id))
async def check_student(
    interaction: discord.Interaction,
    student_id: Optional[str] = None,
    user: Optional[discord.User] = None
) -> None:
    if not await is_staff_or_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission to run this command.", ephemeral=True)
        return

    if not student_id and not user:
        await interaction.response.send_message("❌ Please specify either a student_id or a user.", ephemeral=True)
        return

    record = None
    if user:
        record = await asyncio.to_thread(database.get_student_by_discord_id, user.id)
    elif student_id:
        record = await asyncio.to_thread(database.get_student_by_id, student_id)

    if not record:
        await interaction.response.send_message("❌ No verification record found.", ephemeral=True)
        return

    discord_id = record["discord_id"]
    timestamp = record.get("timestamp", "N/A")
    student_id_hash = record.get("student_id_hash", "N/A")

    user_state = await asyncio.to_thread(database.get_user_state, str(discord_id))
    if user_state:
        strikes, is_locked = user_state
        status = "Locked" if is_locked else f"Active ({strikes} strikes)"
    else:
        status = "Verified"

    embed = discord.Embed(
        title="Student Verification Info",
        color=discord.Color.blue()
    )
    embed.add_field(name="Registered User", value=f"<@{discord_id}> ({discord_id})", inline=False)
    embed.add_field(name="Student ID Hash", value=student_id_hash, inline=False)
    embed.add_field(name="Registration Date", value=timestamp, inline=True)
    embed.add_field(name="Current Status", value=status, inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

    # Automated Support Audit Log Embed to MOD_LOG_CHANNEL_ID
    log_embed = discord.Embed(
        title="🔍 Support Audit Log: Student Inspected",
        description=f"Moderator {interaction.user.mention} inspected verification status.",
        color=discord.Color.blue()
    )
    if user:
        log_embed.add_field(name="Inspected User", value=f"<@{user.id}> ({user.id})", inline=True)
    if student_id:
        log_embed.add_field(name="Inspected Student ID", value=student_id, inline=True)
    await send_audit_log(interaction.guild, log_embed)


@bot.tree.command(name="unlink-student", description="Remove a student's verification record and role by Student ID or User.")
@app_commands.describe(student_id="Raw Student ID", user="Discord user to unlink")
@app_commands.checks.cooldown(1, 60.0, key=lambda i: (i.guild_id or 0, i.user.id))
async def unlink_student(
    interaction: discord.Interaction,
    student_id: Optional[str] = None,
    user: Optional[discord.User] = None
) -> None:
    if not await is_staff_or_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission to run this command.", ephemeral=True)
        return

    if not student_id and not user:
        await interaction.response.send_message("❌ Please specify either a student_id or a user.", ephemeral=True)
        return

    target_discord_id = None
    deleted = False

    if user:
        target_discord_id = user.id
        deleted = await asyncio.to_thread(database.delete_student_by_discord_id, user.id)
    elif student_id:
        record = await asyncio.to_thread(database.get_student_by_id, student_id)
        if record:
            target_discord_id = record["discord_id"]
        deleted = await asyncio.to_thread(database.delete_student_record, student_id)

    if not deleted and not target_discord_id:
        await interaction.response.send_message("❌ No record found to unlink.", ephemeral=True)
        return

    if target_discord_id and interaction.guild:
        verified_role_id = int(os.environ.get("VERIFIED_ROLE_ID", 0))
        role = interaction.guild.get_role(verified_role_id)
        member = await fetch_member_safely(interaction.guild, target_discord_id)
        if member and role and role in member.roles:
            try:
                await member.remove_roles(role)
            except Exception as e:
                print(f"Failed to remove role on unlink: {e}")

    await interaction.response.send_message("✅ Unlinked Student ID/User. They can now re-run verification.", ephemeral=True)

    # Automated Support Audit Log Embed to MOD_LOG_CHANNEL_ID
    log_embed = discord.Embed(
        title="🗑️ Support Audit Log: Student Unlinked",
        description=f"Moderator {interaction.user.mention} unlinked verification record.",
        color=discord.Color.red()
    )
    if user:
        log_embed.add_field(name="Unlinked User", value=f"<@{user.id}> ({user.id})", inline=True)
    if student_id:
        log_embed.add_field(name="Unlinked Student ID", value=student_id, inline=True)
    await send_audit_log(interaction.guild, log_embed)


@bot.tree.command(name="force-verify", description="Manually force-verify a user with a Student ID.")
@app_commands.describe(user="Discord user to verify", student_id="Student ID to assign")
@app_commands.checks.cooldown(1, 60.0, key=lambda i: (i.guild_id or 0, i.user.id))
async def force_verify(
    interaction: discord.Interaction,
    user: discord.User,
    student_id: str
) -> None:
    if not await is_staff_or_admin(interaction):
        await interaction.response.send_message("❌ You do not have permission to run this command.", ephemeral=True)
        return

    # Delete existing link for user or student_id first (offloaded)
    await asyncio.to_thread(database.delete_student_by_discord_id, user.id)
    await asyncio.to_thread(database.delete_student_record, student_id)

    # Force register and unlock (offloaded)
    await asyncio.to_thread(database.add_verified_user, str(user.id), student_id)
    await asyncio.to_thread(database.unlock_user, str(user.id))

    if interaction.guild:
        verified_role_id = int(os.environ.get("VERIFIED_ROLE_ID", 0))
        role = interaction.guild.get_role(verified_role_id)
        member = await fetch_member_safely(interaction.guild, user.id)
        if member and role:
            try:
                await member.add_roles(role)
            except Exception as e:
                print(f"Failed to assign role on force-verify: {e}")

    await interaction.response.send_message(f"✅ Manually verified <@{user.id}> with Student ID {student_id}.", ephemeral=True)

    # Automated Support Audit Log Embed to MOD_LOG_CHANNEL_ID
    log_embed = discord.Embed(
        title="✅ Support Audit Log: Student Force Verified",
        description=f"Moderator {interaction.user.mention} force-verified a student.",
        color=discord.Color.green()
    )
    log_embed.add_field(name="Force Verified User", value=f"<@{user.id}> ({user.id})", inline=True)
    log_embed.add_field(name="Assigned Student ID", value=student_id, inline=True)
    await send_audit_log(interaction.guild, log_embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """
    Global app command error handler to capture cooldown exceptions.
    """
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⚠️ This command is on cooldown. Please try again in {error.retry_after:.1f} seconds.",
            ephemeral=True
        )
    else:
        print(f"App command error: {error}")
        try:
            await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)
        except Exception:
            pass


# ==================== BOT EVENTS ====================

@bot.event
async def on_ready() -> None:
    database.reset_all_locks()
    print(f"[GATEWAY] Logged in as {bot.user} (ID: {bot.user.id if bot.user else 'Unknown'})", flush=True)

@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    print(f"[GATEWAY EVENT] Message received from {message.author} (ID: {message.author.id}) | Is DM: {is_dm} | Attachments: {len(message.attachments)}", flush=True)

    # Check if DM channel
    if message.guild is not None:
        await bot.process_commands(message)
        return

    # If it's a DM, make sure there is an attachment
    if not message.attachments:
        if is_dm:
            print(f"[IGNORED] User {message.author.id} sent text without an image attachment in DMs: {message.content!r}", flush=True)
        return

    user_id = message.author.id
    user_id_str = str(user_id)
    
    # In-flight DM Locking: check if user is already being processed or if locked in DB
    if user_id in bot.processing_users:
        await message.reply("❌ Please wait until your current document analysis completes.")
        return

    user_state = await asyncio.to_thread(database.get_user_state, user_id_str)
    if user_state is not None:
        strikes, is_locked = user_state
        if is_locked:
            print(f"[REJECTED] User {message.author.id} is LOCKED in database. Prompting user...", flush=True)
            try:
                await message.reply("🔒 Your verification is currently locked due to previous failed attempts. Please wait for staff to review your submission in #pending-submissions.")
            except Exception:
                pass
            return

    # Process first attachment
    attachment = message.attachments[0]
    
    # MIME type validation: accept ONLY image/jpeg or image/png
    is_valid_type = False
    if attachment.content_type:
        is_valid_type = attachment.content_type in ["image/jpeg", "image/png"]
    else:
        ext = os.path.splitext(attachment.filename)[1].lower()
        is_valid_type = ext in [".jpg", ".jpeg", ".png"]

    # Ephemeral reject for unsupported file formats
    if not is_valid_type:
        await message.reply("❌ Invalid file. Please upload a JPG or PNG image under 8MB.")
        return

    # Pre-download check: Inspect attachment.size against 8MB limit
    if attachment.size > MAX_FILE_SIZE:
        try:
            await message.author.send("⚠️ File too large. Maximum size is 25MB.")
        except Exception:
            try:
                await message.reply("⚠️ File too large. Maximum size is 25MB.")
            except Exception:
                pass
        return

    # Add to processing_users and queue
    bot.processing_users.add(user_id)
    await verification_queue.put(message)
    await message.reply(f"⏳ Added to the verification queue! You are currently position: {verification_queue.qsize()}.")


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    """
    Prevents audit log spoofing or deletion.
    If an audit log message from MOD_LOG_CHANNEL_ID is deleted, re-sends it and notifies owner.
    """
    if message.id in bot.sent_audit_logs:
        original_embed = bot.sent_audit_logs[message.id]
        guild = message.guild
        
        # If message.guild is not populated, resolve via environment variable
        guild_id = int(os.environ.get("GUILD_ID", 0))
        if not guild and guild_id:
            guild = bot.get_guild(guild_id)
            
        if not guild:
            return
            
        mod_log_id = int(os.environ.get("MOD_LOG_CHANNEL_ID", 0))
        mod_channel = guild.get_channel(mod_log_id)
        if not mod_channel:
            try:
                mod_channel = await guild.fetch_channel(mod_log_id)
            except Exception:
                pass
                
        if mod_channel:
            # Determine appropriate recipient to notify (tag Server Owner or fallback)
            owner_mention = guild.owner.mention if (guild and guild.owner) else "@Server Owner"
            
            warning_embed = discord.Embed(
                title="🚨 SECURITY ALERT: Audit Log Tampered",
                description=(
                    f"An audit log message was deleted! Re-creating original log below.\n"
                    f"Attention: {owner_mention}"
                ),
                color=discord.Color.red()
            )
            # Re-send security alert warning
            await mod_channel.send(
                content=f"⚠️ {owner_mention} **SECURITY ALERT: Audit Log Tampered**",
                embed=warning_embed
            )
            # Re-create and cache the original log embed
            new_msg = await mod_channel.send(embed=original_embed)
            bot.sent_audit_logs[new_msg.id] = original_embed


if __name__ == "__main__":
    import preflight
    # Run gatekeeper checks prior to establishing gateway connection
    try:
        if not (preflight.check_env_variables() and preflight.check_database() and preflight.check_gemini_api()):
            raise RuntimeError("Preflight validation checks failed.")
    except Exception as e:
        print(f"[FATAL] Preflight validation failed: {e}")
        exit(1)
    
    TOKEN = os.getenv("DISCORD_TOKEN")
    bot.run(TOKEN)
