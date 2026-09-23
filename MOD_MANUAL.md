# 🛡️ Community Moderator & Staff Manual

**System:** ALPHA ORG Automated SAI Verification Microservice  
**Audience:** Community Moderators, Support Staff, and Server Administrators ("Splash")  
**Scope:** Staff Operations, Manual Ticket Triage, Escalation Protocols, and Slash Commands  

---

## 📌 Introduction & Operational Philosophy

The **ALPHA ORG Automated SAI Verification microservice** provides automated, high-assurance verification for students submitting their Student Assessment Invoice (SAI). Designed as a drop-in 1:1 replacement for the legacy "Appy" bot UI workflow, it automates over 95% of student verifications while maintaining absolute administrative control for non-technical community moderators and staff.

### Core Staff Responsibilities
* **Deployment**: Deploying and maintaining the verification gateway in public channels.
* **Triage**: Reviewing escalated tickets in `#pending-submissions` for student documents requiring human review.
* **Support**: Handling edge cases (e.g., damaged documents, account transfers) via staff slash commands.
* **Audit Oversight**: Monitoring automated log entries in `#mod-logs` to detect credential sharing or duplicate submissions.

---

## 🚀 1. System Deployment & Gateway Setup

To enable verification for members, a Server Administrator or Support Staff member must post the interactive verification gateway in the public verification channel.

### Step-by-Step Gateway Deployment
1. Navigate to the designated public channel (`#verify`).
2. Type and execute the following slash command in the message box:
   ```text
   /setup-verify
   ```
3. The bot will validate your staff permissions and display an ephemeral confirmation message visible only to you:
   > `✅ Verification dropdown posted successfully!`
4. A public verification card titled **"Student Verification Required"** will appear in `#verify` featuring the **"Apply for Verification"** dropdown menu.

> ℹ️ **Operational Note:** Running `/setup-verify` again will post a fresh dropdown card. Previous dropdown cards remain active unless deleted.

---

## 🔄 2. Student Verification Lifecycle & 2-Strike AI Logic

The bot mimics the legacy "Appy" UI sequence to ensure zero student friction while protecting server security and Google Gemini AI processing quotas.

```
+-----------------------------------------------------------------------+
|                         STUDENT WORKFLOW                              |
+-----------------------------------------------------------------------+
|  1. Select "Apply for Verification" dropdown in public #verify        |
|  2. Receive ephemeral notice -> Bot dispatches Private DM             |
|  3. Click [Start Verification] in DM -> Upload SAI Document Image      |
+-----------------------------------------------------------------------+
                                   |
                                   v
                      +-------------------------+
                      | Automated AI Evaluation |
                      +-------------------------+
                         /                   \
                        /                     \
             [ Verification PASS ]     [ Verification FAIL ]
                      /                         \
                     v                           v
     +-------------------------------+   +-------------------------------+
     | • Assign Verified Role        |   | Increment User Strike Count   |
     | • Send DM Success Embed       |   +-------------------------------+
     | • Log Record to Database      |        /                     \
     +-------------------------------+       /                       \
                                     (Strike 1/2)                 (Strike 2/2)
                                          /                             \
                                         v                               v
                       +---------------------------+   +----------------------------------+
                       | Send DM Warning Embed     |   | • Send DM Lockout Embed          |
                       | Allow Student Re-attempt  |   | • Lock Student Account           |
                       +---------------------------+   | • Escalate to #pending-submissions|
                                                       +----------------------------------+
```

### Student Experience (DM Workflow)
1. **Initiation**: The student selects **"Apply for Verification"** in `#verify`.
2. **Ephemeral Route**: The bot responds ephemerally in `#verify` and dispatches a private Direct Message to the student containing **[Start Verification]** and **[Cancel]** buttons.
3. **Document Submission**: Clicking **[Start Verification]** prompts the student to upload an image (JPEG, PNG, or WebP) of their official Student Assessment Invoice (SAI).
4. **AI Assessment**: The automated AI vision pipeline analyzes the document in under 5 seconds.

### The 2-Strike Rule Explained
To defend against quota exhaustion and malicious image spamming, the system enforces a strict 2-strike system:

* **Pass (Success)**:
  The student's document is authenticated, their 9-digit Student ID is extracted and stored, the verified server role (`Alpha Member .ᐟ` / `Verified Student`) is assigned, and a green confirmation embed is sent in DMs.
* **Strike 1 (Warning)**:
  If the student uploads an unreadable image, cropped document, or incorrect file type, the bot issues a yellow **"Verification Warning (Strike 1/2)"** embed in DMs detailing the failure reason. The student is granted **one additional attempt**.
* **Strike 2 (Lockout & Escalation)**:
  If a second consecutive upload fails, the student's account is **locked from further automated attempts** to prevent API abuse. The bot dispatches a red **"Verification Locked"** embed in DMs informing the student that their ticket has been forwarded to server staff for manual review.

---

## 📥 3. Manual Triage Protocol (`#pending-submissions`)

When a student reaches Strike 2, their verification request is automatically forwarded to the private staff channel `#pending-submissions` as a ticket embed accompanied by the original document image buffer.

```
======================================================================
                  [MANUAL VERIFICATION TICKET EMBED]
======================================================================
Title:       Manual Verification Required
Description: User has accumulated 2 strikes and is locked. Please review...
Fields:
  • User Mention:  @StudentUsername
  • User ID:       102938475610293847
  • Student ID:    2024-00192 (or N/A if unreadable)
  • Failure Reason: UNREADABLE_TEXT / CROPPED_IMAGE
Attached File: sai_document.jpg
[ Accept ]   [ Deny ]
======================================================================
```

### Review Guidelines & Action Buttons

Staff members holding the `@Support` role or Administrator permissions can resolve tickets using the embedded action buttons. Every button click performs a live API privilege verification to prevent role spoofing.

#### Option A: Approving a Ticket (**[Accept]**)
1. Inspect the attached `sai_document.jpg` image in `#pending-submissions`.
2. Verify that the image contains a legitimate STI College Student Assessment Invoice showing a valid student name and 9-digit Student ID.
3. Click the green **[Accept]** button on the ticket embed.
4. **System Action**:
   * The bot verifies that the extracted Student ID is not already registered to another user.
   * The student's account state is **unlocked** and registered as verified in the system database.
   * The bot assigns the verified server role to the student in Discord.
   * The student receives a private DM: *"🎉 Congratulations! Your student verification has been approved by staff."*
   * The staff ticket embed turns **Green**, displays *"✅ Approved by @YourUsername"*, and disables further button interaction.

> ⚠️ **Duplicate Prevention Shield:** If the Student ID on the ticket is already registered to a different Discord account, clicking **[Accept]** will block the approval, turn the embed **Red**, and display a duplicate warning to prevent credential sharing.

#### Option B: Denying a Ticket (**[Deny]**)
1. If the attached image is fake, severely corrupted, or completely irrelevant, click the red **[Deny]** button on the ticket embed.
2. **System Action**:
   * The student's account lock is **cleared** and their strikes are reset, allowing them to attempt automated verification again in the future.
   * The student receives a private DM: *"❌ Your student verification request was denied by staff. You may try re-submitting your document."*
   * The staff ticket embed turns **Red**, displays *"❌ Denied by @YourUsername"*, and disables further button interaction.

---

## ⏱️ 4. Anti-Spam & Cooldown Rules

To preserve system stability and prevent button-flooding attacks, all interactive UI elements carry thread-safe rate limiting:

* **Global Interaction Cooldown**: Every user is subject to a strict **60-second cooldown** between button clicks and dropdown selections.
* **Cooldown Notification**: If a user clicks a button or re-selects the dropdown before 60 seconds have elapsed, the bot suppresses the request and responds ephemerally:
  > `⚠️ Please wait 42.5 seconds before requesting verification again.`

---

## 🛠️ 5. Staff Slash Command Toolkit

Authorized staff members (`@Support` or Administrators) have access to three administrative slash commands for auditing and handling manual edge cases.

> ⚠️ **IMPORTANT:** All staff commands enforce a 60-second execution cooldown per user and **automatically emit an immutable audit log embed** into `#mod-logs` tracking the moderator's action, target user, and timestamp.

---

### Command 1: `/check-student`

Inspects the current verification record, registration date, strike count, and lock status of a student.

#### Command Syntax
```text
/check-student [student_id: Optional] [user: Optional]
```

#### Parameters
* `student_id` *(Optional)*: The raw 9-digit Student ID string (e.g., `2024-00192`).
* `user` *(Optional)*: The `@mention` or Discord User object of the student.
*(At least one parameter must be provided).*

#### Step-by-Step Usage
1. Type `/check-student` in any staff channel.
2. Select either the `user` or `student_id` option and provide the target.
3. Press **Enter**.

#### Expected Output
The bot responds ephemerally with a **"Student Verification Info"** embed:
* **Registered User**: `<@StudentUserID> (102938475610293847)`
* **Student ID Hash**: Cryptographically anonymized record hash.
* **Registration Date**: Timestamp of verification (e.g., `2026-09-23 11:15:00`).
* **Current Status**: Displays `Verified`, `Active (1 strike)`, or `Locked`.

---

### Command 2: `/unlink-student`

**DESTRUCTIVE ACTION:** Revokes a student's verification status, removes their verified role, and purges their database registration record. This allows the student (or another user) to re-verify using that Student ID.

#### Command Syntax
```text
/unlink-student [student_id: Optional] [user: Optional]
```

#### Parameters
* `student_id` *(Optional)*: The raw 9-digit Student ID to remove.
* `user` *(Optional)*: The Discord member to unlink.
*(At least one parameter must be provided).*

#### Operational Guidelines
* Use **`/unlink-student`** when a student accidentally verified on an alt account, transferred Discord accounts, or provided incorrect information during manual review.

#### Step-by-Step Usage
1. Type `/unlink-student` in a staff channel.
2. Specify the target `user` or `student_id`.
3. Press **Enter**.

#### Expected Output
* Ephemeral Confirmation: `✅ Unlinked Student ID/User. They can now re-run verification.`
* Server Action: The verified role is automatically stripped from the member's Discord profile.
* Audit Log Emission: An embed titled **"🗑️ Support Audit Log: Student Unlinked"** is dispatched to `#mod-logs`.

---

### Command 3: `/force-verify`

**ADMINISTRATIVE OVERRIDE:** Manually registers and verifies a student, instantly granting them the verified role and bypassing all AI document validation checks.

#### Command Syntax
```text
/force-verify [user: Required] [student_id: Required]
```

#### Parameters
* `user` *(Required)*: The target Discord member `@mention` to verify.
* `student_id` *(Required)*: The Student ID string to bind to this user.

#### Operational Guidelines
* Use **`/force-verify`** when a student provides an extremely damaged, handwritten, or physical document that cannot be processed by automated vision scanning, but has been manually validated by staff.

#### Step-by-Step Usage
1. Type `/force-verify` in a staff channel.
2. Select the target `user` and enter the target `student_id`.
3. Press **Enter**.

#### Expected Output
* Ephemeral Confirmation: `✅ Manually verified <@User> with Student ID 2024-00192.`
* Server Action: Any prior conflicting locks/links for that user or Student ID are cleared, the user's account state is unlocked, and the verified role is assigned.
* Audit Log Emission: An embed titled **"✅ Support Audit Log: Student Force Verified"** is dispatched to `#mod-logs`.

---

## 📑 Quick-Reference Summary Table for Staff Commands

| Slash Command | Arguments | Permissions Required | Primary Use Case | Mod Log Audit Title |
| :--- | :--- | :--- | :--- | :--- |
| **`/setup-verify`** | *None* | Admin / `@Support` | Deploy public verification card in `#verify` | *N/A (Console log)* |
| **`/check-student`** | `user` OR `student_id` | Admin / `@Support` | Check registration date, strikes, & status | `🔍 Support Audit Log: Student Inspected` |
| **`/unlink-student`** | `user` OR `student_id` | Admin / `@Support` | **Revoke role & wipe record** for re-registration | `🗑️ Support Audit Log: Student Unlinked` |
| **`/force-verify`** | `user` AND `student_id` | Admin / `@Support` | **Override AI** and manually verify damaged documents | `✅ Support Audit Log: Student Force Verified` |

---

## 🚨 Emergency Escalation Procedures

If the verification system behaves unexpectedly, follow these escalation steps:

1. **Bot Not Responding to Commands**: Check if the bot status indicator in Discord is online. If offline, notify the Server Administrator to inspect the host process.
2. **Missing Permissions Error**: Ensure the bot's Discord role is positioned higher in the server's role hierarchy than the Verified Student role (`Alpha Member .ᐟ`).
3. **Pterodactyl Console Escalation**: For technical maintenance or container issues, refer to the technical diagnostic procedures documented in [`QA_SMOKE_TEST.md`](QA_SMOKE_TEST.md).
