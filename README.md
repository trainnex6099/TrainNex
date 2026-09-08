# Trainnex — FTO Training Queue Bot

Trainnex routes approved applicants from a Melony application server to Field
Training Officers (FTOs) in a FIFO queue.

## Your server setup

### Server 1 — Applications
- Server ID: `1516305981078900756`
- Melony approval channel: `1516311261267099741`
- Melony bot ID: `1043430551601811456`

### Server 2 — Training
- Server ID: `1445916050003591240`
- FTO role: `1445916050427216002`
- Fallback announcement channel: `1445916053157707790`
- Claim notification channel: `1445916053715554334`
- FTO Commander: `1546687246529462292`
- FTO Overseer: `1546687199120986222`

## What it does

1. Watches only the configured Melony approval channel.
2. Requires the message author to be the configured Melony bot.
3. Detects the `WCSO Department Application | Application Approved` embed.
4. Extracts the trainee's Discord ID from a real Discord mention.
5. Saves Melony's message jump URL.
6. Sends the next available FTO a DM with:
   - Trainnex embed/logo
   - trainee mention
   - `View Approval & Results` hyperlink
   - Discord relative expiration countdown
   - `CLAIM` and `PASS ON` buttons
7. Claim moves the FTO to the back of the queue and posts a claim notification.
8. Pass/timeout moves the FTO to the back and routes the trainee to the next FTO.
9. Each FTO gets one opportunity for a particular trainee. If everyone passes,
   times out, or cannot be DM'd, Trainnex posts an open claim announcement.
10. The open claim announcement has only a `CLAIM` button.
11. The first FTO to claim it gets the trainee. The announcement is edited to
    show who claimed it, then the claim notification is posted.
12. Queue/offer state is stored in `data.json`, so restarts do not erase it.
13. Persistent buttons are re-registered after restarts.

## Python setup

Recommended for your existing Windows/VS Code setup:

```powershell
cd "C:\path\to\Trainnex"

py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and put the bot token in `.env`.

Then:

```powershell
python main.py
```

## Discord Developer Portal

Add the bot to BOTH servers.

The bot needs access to the Melony approval channel in Server 1 and the
training channels in Server 2.

For the message listener, enable the **Message Content Intent** and
**Server Members Intent** for the bot in the Developer Portal.

The bot uses message content only to identify Melony's approval embed. It does
not process ordinary chat messages.

## Queue commands

In Server 2:

```text
/trainner add @Trainer
/trainner remove @Trainer
/trainner queue
/trainner status
/trainner reset
```

`add`, `remove`, `status`, and `reset` require either:
- FTO Commander
- FTO Overseer
- Administrator

`queue` is available to FTOs and queue managers.

## Important notes

### Trainee membership
For the best Discord mention behavior in Server 2, trainees should also be
members of Server 2. Trainnex can still DM a trainee through the mutual
application server, but a user who is not in the training server may not render
as a normal clickable server mention there.

### Melony message format
The parser is designed around the screenshot you supplied:
`WCSO Department Application | Application Approved`

It first looks for a real Discord mention such as `<@123456789>`.
It also has a fallback for a username shown inside parentheses.

If Melony changes its embed format, update `parse_trainee_id()` in `main.py`.

### Logo
`assets/trainnex_logo.png` is the Trainnex circular logo supplied for this
project. It is attached to embeds and displayed as the embed thumbnail.

### Token safety
Never put your bot token in `main.py`, screenshots, Discord messages, GitHub,
or this chat. Keep it in `.env`.
