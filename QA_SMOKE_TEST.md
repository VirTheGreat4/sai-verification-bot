# 🧪 QA Smoke Test Execution Matrix

**System Target:** ALPHA ORG Automated SAI Verification Microservice  
**Environment:** Discord GUI Client & Pterodactyl Container Console  
**Target Resource Envelope:** 256MB RAM Container Ceiling  
**Testing Scope:** End-to-End Live Environment Functional & Performance Validation  

---

## 📋 Executive Overview & Test Objectives

This document establishes the deterministic QA smoke testing procedures and validation matrices for the **ALPHA ORG Automated SAI Verification microservice**.

### Key Architectural Constraints Under Test
1. **256MB Memory Ceiling**: Ensure image stream processing and real-time garbage collection prevent Out-Of-Memory (OOM) termination (**Exit Code 137**) under maximum payload stress (up to 24MB / 64 Megapixel inputs).
2. **1:1 Legacy "Appy" UI/UX Mimicry**: Confirm uninterrupted transition from the public `#verify` channel dropdown to private DMs.
3. **Quota Protection & 2-Strike Escalation**: Verify that unverified or erroneous submissions trigger warnings at Strike 1 and escalate with DB lockout at Strike 2.
4. **Anti-Spoofing Staff Fallback**: Verify that staff triage actions in `#pending-submissions` bypass Discord client cache via live API queries and safely mutate database state.

---

## 🧭 Pre-Flight Test Environment Setup

Prior to executing the test matrix, ensure the following preconditions are met:

* **Pterodactyl Console**: Open and tailing stdout in real time.
* **Test Discord Accounts**:
  * **Tester User (Student Persona)**: Non-admin Discord account with direct messages enabled (`User Settings` > `Privacy & Safety` > `Allow direct messages from server members` = **ON**).
  * **Staff User (Moderator Persona)**: Discord account assigned the authorized `@Support` or `@Admin` role.
  * **Unauthorized User**: Regular server member without support permissions.
* **Target Channels**:
  * `#verify`: Public verification gateway channel.
  * `#pending-submissions`: Restricted moderator review channel.
  * `#mod-logs`: Moderator audit log channel.
* **Test Image Fixtures**:
  * `Fixture-A_Valid_SAI.jpg`: Clean, legible Student Assessment Invoice (< 5MB, JPEG).
  * `Fixture-B_HighRes_SAI.jpg`: High-density 24MB, 64MP smartphone camera capture.
  * `Fixture-C_Invalid_Document.png`: Image of an irrelevant object, meme, or blank canvas.
  * `Fixture-D_Corrupted_Header.bin`: Binary file with invalid magic bytes renamed to `.jpg`.

---

## 📊 End-to-End QA Execution Matrix

| Test ID | Phase & Objective | Step-by-Step Execution Procedure | Expected Result Criteria | Actual Result (Pass/Fail) | Telemetry & Resource Assertions |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **ST-01** | **Phase 1: Initialization**<br>Verify public gateway deployment | 1. As Staff, execute slash command `/setup-verify` in `#verify`.<br>2. Observe interaction response and `#verify` message embed. | • Interaction responds with ephemeral success confirmation.<br>• Bot posts public embed with title **"Student Verification Required"**.<br>• Persistent dropdown **"Apply for Verification"** is rendered. | `[ PASS / FAIL ]` | • Console logs command execution by staff member.<br>• No Discord API rate limit or permission errors. |
| **ST-02** | **Phase 1: Gateway Routing**<br>Trigger DM verification session | 1. As Student, select **"Apply for Verification"** from dropdown in `#verify`.<br>2. Check Discord Direct Messages. | • Bot displays ephemeral notice in `#verify`: *"Verification instructions have been sent to your DMs!"*<br>• Bot sends DM containing **"Student Verification"** embed with **[Start Verification]** and **[Cancel]** buttons. | `[ PASS / FAIL ]` | • Log shows `[GATEWAY EVENT]` interaction received for user ID.<br>• Gateway session initiated. |
| **ST-03** | **Phase 1: Cooldown Defense**<br>Validate 60s dropdown spam guard | 1. Immediately re-select **"Apply for Verification"** within 60 seconds of ST-02. | • Interaction rejects spam with ephemeral warning: *"⚠️ Please wait X.X seconds before requesting verification again."*<br>• No duplicate DMs are dispatched. | `[ PASS / FAIL ]` | • Console notes rate limit cooldown trigger.<br>• Memory usage remains steady. |
| **ST-04** | **Phase 1: DM Direct Engagement**<br>Prompt document upload | 1. In DM with Bot, click **[Start Verification]**.<br>2. Observe response. | • Bot responds with **"Upload Document"** prompt instructing user to upload SAI document (JPG/PNG). | `[ PASS / FAIL ]` | • Gateway updates interaction state.<br>• User placed in active waiting state. |
| **ST-05** | **Phase 2: Optimal Flow**<br>Valid SAI auto-approval | 1. In DM, upload `Fixture-A_Valid_SAI.jpg`.<br>2. Start timer upon file upload completion.<br>3. Inspect response embed and server roles. | • Bot acknowledges with: *"⏳ Analyzing your SAI document..."*<br>• Processing completes in **< 5 seconds**.<br>• Bot edits status to *"✅ Analysis complete!"* and posts **"Verification Successful"** embed with extracted Student ID.<br>• Student account is automatically granted the verified role (`Alpha Member .ᐟ` / `Verified Student`) in the guild. | `[ PASS / FAIL ]` | • Console logs: `[WORKER] Picked up verification job for user...`<br>• AI model returns `PASS`.<br>• SQLite WAL commits hashed record.<br>• Role successfully granted via Discord API. |
| **ST-06** | **Phase 3: High-Density Stress**<br>24MB / 64MP payload handling | 1. Using a new unverified test account, initiate verification in DM.<br>2. Upload `Fixture-B_HighRes_SAI.jpg` (24MB, 64MP).<br>3. Monitor Pterodactyl container RAM graphs and stdout. | • Bot accepts attachment without crashing.<br>• Image is processed without encountering OOM kill (**Exit Code 137**).<br>• Verification completes successfully within allowable limits. | `[ PASS / FAIL ]` | • **RAM Footprint Assertion**: Active RAM allocation during stream downscaling must remain **< 40MB**.<br>• Garbage collection immediately purges memory arrays post-execution.<br>• Zero OOM container restarts. |
| **ST-07** | **Phase 3: Invalid Payload**<br>Magic byte header validation | 1. In DM, upload `Fixture-D_Corrupted_Header.bin`. | • Bot rejects document with status: *"❌ Invalid file. The uploaded image does not have a valid file signature (JPEG, PNG, or WebP)."*<br>• Request does not query the AI pipeline. | `[ PASS / FAIL ]` | • Console logs `[REJECTED]` invalid magic bytes signature.<br>• Zero token consumption on AI API. |
| **ST-08** | **Phase 3: Strike 1 Handling**<br>Irrelevant document warning | 1. Using a new unverified account, initiate verification in DM.<br>2. Upload `Fixture-C_Invalid_Document.png`. | • Bot responds with **"Verification Warning (Strike 1/2)"** yellow embed.<br>• Failure reason is clearly specified (e.g., Unreadable / Non-SAI document).<br>• Student is informed that one attempt remains before lockout. | `[ PASS / FAIL ]` | • Console logs `[WORKER]` evaluation status `FAIL`.<br>• SQLite user state increments strike counter to `1` with locked status `0`. |
| **ST-09** | **Phase 3: Strike 2 & Lockout**<br>Secondary failure & staff escalation | 1. In DM with same account from ST-08, re-upload `Fixture-C_Invalid_Document.png`.<br>2. Check DM response.<br>3. Check `#pending-submissions` channel. | • In DM: Bot displays red **"Verification Locked"** embed stating account has accumulated 2 strikes and is forwarded to staff.<br>• In `#pending-submissions`: Bot emits **"Manual Verification Required"** orange embed containing User ID, extracted data, failure reason, and interactive **[Accept]** and **[Deny]** buttons.<br>• Image is re-attached as a native memory buffer (`discord.File`) ensuring permanent accessibility without expiring CDN URLs. | `[ PASS / FAIL ]` | • Console logs user strike threshold reached (Strikes: `2`, Locked: `1`).<br>• Ticket dispatched to `#pending-submissions`.<br>• User locked in SQLite database. |
| **ST-10** | **Phase 3: Duplicate Student ID**<br>Prevent duplicate credential abuse | 1. Using another distinct unverified account, upload `Fixture-A_Valid_SAI.jpg` (already verified in ST-05). | • Bot rejects verification: *"Duplicate verification: Student ID is already registered to another user."*<br>• Bot emits an audit alert embed in `#mod-logs` notifying moderators of duplicate registration attempt. | `[ PASS / FAIL ]` | • Console logs duplicate hash detection via HMAC comparison.<br>• Atomic TOCTOU lock prevents duplicate record insertion.<br>• Mod log alert logged in `#mod-logs`. |
| **ST-11** | **Phase 4: Staff Escalation - Deny**<br>Manual denial and lockout reset | 1. As Staff in `#pending-submissions`, click **[Deny]** on ticket generated in ST-09.<br>2. Check staff channel embed state.<br>3. Check Student DM. | • Staff button updates: Buttons are disabled; embed turns Red with status *"❌ Denied by @StaffUser"*.<br>• Student receives DM: **"Verification Denied"** indicating they may re-attempt verification.<br>• Student state in SQLite is unlocked and strikes reset. | `[ PASS / FAIL ]` | • Staff interaction verified via live Discord API privilege fetch.<br>• SQLite database unlocks student record.<br>• Audit event logged. |
| **ST-12** | **Phase 4: Staff Escalation - Accept**<br>Manual override & role grant | 1. On an escalated ticket in `#pending-submissions`, as Staff click **[Accept]**.<br>2. Inspect staff channel embed.<br>3. Inspect Student DM and guild roles. | • Staff embed turns Green with status *"✅ Approved by @StaffUser"*; buttons disabled.<br>• Student receives **"Verification Approved"** DM embed.<br>• Student is granted the verified server role in Discord.<br>• Student record is permanently committed to database. | `[ PASS / FAIL ]` | • Live API anti-spoofing check asserts staff authority.<br>• SQLite marks record verified and clears lock.<br>• Role assignment confirmed via Discord API. |
| **ST-13** | **Phase 4: Unauthorized Staff Click**<br>Zero-permission security check | 1. As an unauthorized regular member, click **[Accept]** or **[Deny]** on a staff embed in `#pending-submissions`. | • Interaction fails with ephemeral response: *"❌ No permission."*<br>• Embed and database state remain completely unchanged. | `[ PASS / FAIL ]` | • Discord API member fetch evaluates roles against authorized ID.<br>• Interaction rejected without state mutation. |
| **ST-14** | **Phase 5: Staff Toolkit Commands**<br>Verify slash commands & audit trail | 1. Execute `/check-student` for verified student.<br>2. Execute `/unlink-student` for verified student.<br>3. Execute `/force-verify` for unverified test user. | • `/check-student`: Returns ephemeral card with User ID, Student ID Hash, Registration Date, and Status.<br>• `/unlink-student`: Removes verified role, purges database record, and sends confirmation.<br>• `/force-verify`: Bypasses AI, registers student, assigns role, and confirms.<br>• All three commands dispatch automated embeds to `#mod-logs`. | `[ PASS / FAIL ]` | • Slash command cooldowns (60s) enforced.<br>• All staff actions logged with moderator mention and timestamp in `#mod-logs`. |

---

## 📡 Phase 5: Pterodactyl Console Telemetry Guide

Monitor the container terminal output to ensure normal operational metrics during load:

```text
================================================================================
[PTERODACTYL TELEMETRY STREAM]
================================================================================
[GATEWAY EVENT] Persistent views registered and slash commands synced.
[WORKER] Picked up verification job for user 102938475610293847. Starting analysis...
[IMAGE OPTIMIZER] JPEG Draft stream downscaled: 4032x3024 -> 1600x1200 (Memory delta: +4.2MB)
[GARBAGE COLLECT] Explicit gc.collect() executed. Heap delta purged.
[AI ENGINE] Querying Gemini Flash model pool. Active model: gemini-3.5-flash
[AI ENGINE] Verification outcome: PASS (Latency: 1.84s)
[DATABASE] Committed HMAC hash for student 2024-00192 to WAL.
[DISCORD API] Assigned role 'Alpha Member .ᐟ' (ID: 112233445566778899) to User ID 102938475610293847.
[WORKER] Verification cycle complete for user 102938475610293847.
================================================================================
```

### 🚨 Critical Diagnostic Signals & Remediation

| Console Log Signal | Failure Mode / Implication | Immediate Action |
| :--- | :--- | :--- |
| `Container received signal 9 / Exit Code 137` | **Fatal Out-Of-Memory (OOM)**: Memory allocation exceeded 256MB container hard cap. | Verify streaming draft decoding is operating and garbage collection is invoked. |
| `[AI ERROR] 429 ResourceExhausted` | **API Rate Limit Reached**: Gemini API quota tier exceeded on active key. | Check key pool rotation logs to confirm automatic failover to the next active key. |
| `403 Forbidden (Missing Permissions)` | **Role Hierarchy Conflict**: Bot's Discord role is positioned below the Verified Student role. | Drag the bot's role higher in Discord Server Settings > Roles. |
| `[REJECTED] Invalid file signature` | **Malicious/Corrupted Upload**: User uploaded non-JPEG/PNG/WebP data. | Normal operation; verify user received rejection DM without crash. |

---

## 📝 QA Execution Sign-Off

| Milestone | Tester Name / Discord Tag | Date (UTC) | Final Status | Signature |
| :--- | :--- | :--- | :--- | :--- |
| **Initial Deployment** | `____________________` | `__________` | `[ PASS / FAIL ]` | `__________` |
| **Edge & Stress Cases** | `____________________` | `__________` | `[ PASS / FAIL ]` | `__________` |
| **Staff Escalations** | `____________________` | `__________` | `[ PASS / FAIL ]` | `__________` |
