import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ============================================================
# Trainnex - FTO Trainer Queue Bot
# ============================================================
# Two-server design:
#   Server 1 = Melony applications
#   Server 2 = FTO/training operations
#
# DO NOT put your Discord bot token directly in this file.
# Put it in .env as:
#   DISCORD_TOKEN=your_token_here
# ============================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# ----------------------------
# Server 1 - Applications
# ----------------------------
APPLICATION_SERVER_ID = 1516305981078900756
APPROVAL_CHANNEL_ID = 1516311261267099741
MELONY_BOT_ID = 1043430551601811456

# ----------------------------
# Server 2 - Training
# ----------------------------
TRAINING_SERVER_ID = 1445916050003591240
FTO_ROLE_ID = 1445916050427216002
ANNOUNCEMENT_CHANNEL_ID = 1445916053157707790
CLAIM_NOTIFICATION_CHANNEL_ID = 1445916053715554334

FTO_COMMANDER_ROLE_ID = 1546687246529462292
FTO_OVERSEER_ROLE_ID = 1546687199120986222

# ----------------------------
# Behavior
# ----------------------------
OFFER_TIMEOUT_SECONDS = 30 * 60
DATA_FILE = Path("data.json")
LOG_FILE = Path("trainnex.log")

EMBED_COLOR = discord.Color.from_rgb(105, 135, 55)  # Trainnex olive
SUCCESS_COLOR = discord.Color.green()
WARNING_COLOR = discord.Color.orange()
ERROR_COLOR = discord.Color.red()
NEUTRAL_COLOR = discord.Color.from_rgb(55, 65, 75)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("Trainnex")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Create a .env file and add DISCORD_TOKEN=..."
    )


def atomic_write_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def load_data() -> dict:
    if not DATA_FILE.exists():
        return {
            "queue": [],
            "offers": {},
            "pending": {},
            "processed_approval_messages": [],
        }

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.exception("Could not read data.json; starting with empty data.")
        data = {}

    data.setdefault("queue", [])
    data.setdefault("offers", {})
    data.setdefault("pending", {})
    data.setdefault("processed_approval_messages", [])
    return data


DATA = load_data()
DATA_LOCK = asyncio.Lock()


def save_data() -> None:
    atomic_write_json(DATA_FILE, DATA)


def role_member(member: discord.Member, role_id: int) -> bool:
    return any(role.id == role_id for role in member.roles)


def is_queue_manager(member: discord.Member) -> bool:
    return (
        member.guild.id == TRAINING_SERVER_ID
        and (
            role_member(member, FTO_COMMANDER_ROLE_ID)
            or role_member(member, FTO_OVERSEER_ROLE_ID)
            or member.guild_permissions.administrator
        )
    )


def has_fto_role(member: discord.Member) -> bool:
    return role_member(member, FTO_ROLE_ID)


def normalize_queue() -> None:
    """Remove duplicate IDs while preserving queue order."""
    seen = set()
    cleaned = []
    for uid in DATA["queue"]:
        try:
            uid = int(uid)
        except (ValueError, TypeError):
            continue
        if uid not in seen:
            cleaned.append(uid)
            seen.add(uid)
    DATA["queue"] = cleaned


def remove_from_queue(user_id: int) -> None:
    DATA["queue"] = [uid for uid in DATA["queue"] if uid != user_id]


def move_to_back(user_id: int) -> None:
    remove_from_queue(user_id)
    DATA["queue"].append(user_id)


def get_next_available_trainer(exclude: Optional[set[int]] = None) -> Optional[int]:
    """
    Finds the first FTO in queue who is not currently handling a pending offer
    and who is still a member of the training server.

    We do not move the trainer here. They move to the back after Claim/Pass/timeout,
    matching the requested queue behavior.
    """
    exclude = exclude or set()

    for user_id in DATA["queue"]:
        if user_id in exclude:
            continue

        # A trainer can only have one active offer at a time.
        if any(
            offer.get("trainer_id") == user_id
            for offer in DATA["offers"].values()
        ):
            continue

        return user_id

    return None


def get_member_by_id(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    return guild.get_member(user_id)


def get_user_mention(user_id: int) -> str:
    return f"<@{user_id}>"


def relative_timestamp(unix_time: int) -> str:
    return f"<t:{unix_time}:R>"


def parse_trainee_id(message: discord.Message, application_guild: discord.Guild) -> Optional[int]:
    """Extract the trainee from Melony's approval message.

    In the real Melony format, the trainee is the user mention ABOVE the
    embed. Discord normally exposes that through message.mentions, but we
    also parse the raw message content so the bot does not depend on mention
    resolution/cache behavior.
    """
    # Preferred: resolved user mentions.
    if message.mentions:
        return message.mentions[0].id

    # More direct: Discord's raw mention IDs, when available.
    raw_mentions = getattr(message, "raw_mentions", None) or []
    if raw_mentions:
        try:
            return int(raw_mentions[0])
        except (ValueError, TypeError):
            pass

    # Fallback: parse the literal <@123> or <@!123> from message content.
    content = message.content or ""
    match = re.search(r"<@!?(\d+)>", content)
    if match:
        return int(match.group(1))

    # Last-resort username/display-name lookup. This is intentionally only
    # used if Melony supplied a username in normal message text.
    match = re.search(r"\(@?([A-Za-z0-9_.-]{2,32})\)", content)
    if match:
        target = match.group(1).lower()
        for member in application_guild.members:
            candidates = {
                member.name.lower(),
                member.display_name.lower(),
                str(member).lower(),
            }
            if target in candidates:
                return member.id

    return None


def get_melony_embed_title(message: discord.Message) -> str:
    """Read Melony's application status from the actual embed format.

    Melony currently places the status in embed.author.name rather than
    embed.title, e.g. ``WCSO Department Application | Response Approved``.
    """
    if not message.embeds:
        return ""

    embed = message.embeds[0]

    # Current Melony format: status is in the embed author name.
    author = getattr(embed, "author", None)
    author_name = (getattr(author, "name", "") or "").strip()
    if author_name:
        return author_name

    # Fallbacks for older/alternate embed formats.
    title = (embed.title or "").strip()
    if title:
        return title

    try:
        raw = embed.to_dict()
        raw_author = raw.get("author") or {}
        return str(raw_author.get("name") or raw.get("title") or "").strip()
    except Exception:
        return ""


def is_melony_approval(message: discord.Message) -> bool:
    if message.guild is None or message.guild.id != APPLICATION_SERVER_ID:
        return False
    if message.channel.id != APPROVAL_CHANNEL_ID:
        return False
    if message.author.id != MELONY_BOT_ID:
        return False
    if not message.embeds:
        return False

    # Melony's real approval embed uses:
    # "WCSO Department Application | Response Approved"
    # Denied/rejected messages are intentionally ignored.
    title = get_melony_embed_title(message).lower()
    return (
        "department application" in title
        and "response approved" in title
    )


class TrainnexBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.expiration_tasks: dict[str, asyncio.Task] = {}
        self.setup_complete = False

    async def setup_hook(self):
        normalize_queue()
        save_data()

        # Register the persistent views for active offers so buttons survive
        # a bot restart.
        for offer_id, offer in list(DATA["offers"].items()):
            if offer.get("state") != "pending":
                continue

            self.add_view(
                OfferView(offer_id),
                message_id=offer.get("dm_message_id"),
            )

        # Persistent fallback-claim views.
        for trainee_id, pending in list(DATA["pending"].items()):
            if pending.get("state") != "open":
                continue

            self.add_view(
                FallbackClaimView(int(trainee_id)),
                message_id=pending.get("announcement_message_id"),
            )

        # Sync slash commands to Server 2.
        training_guild = discord.Object(id=TRAINING_SERVER_ID)
        self.tree.copy_global_to(guild=training_guild)
        await self.tree.sync(guild=training_guild)

        self.setup_complete = True
        log.info("Trainnex slash commands synced to training server.")

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)

        # Resume timers after reconnect/restart.
        for offer_id, offer in list(DATA["offers"].items()):
            if offer.get("state") == "pending":
                self.schedule_offer_expiration(offer_id)

        await self.resume_fallback_announcements()

    def schedule_offer_expiration(self, offer_id: str):
        old = self.expiration_tasks.get(offer_id)
        if old and not old.done():
            old.cancel()

        task = asyncio.create_task(self.expire_offer_when_ready(offer_id))
        self.expiration_tasks[offer_id] = task

    async def expire_offer_when_ready(self, offer_id: str):
        try:
            offer = DATA["offers"].get(offer_id)
            if not offer or offer.get("state") != "pending":
                return

            remaining = float(offer["expires_at"]) - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)

            await expire_offer(offer_id)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Error expiring offer %s", offer_id)

    async def resume_fallback_announcements(self):
        """
        If a fallback announcement was open when the bot restarted, restore its
        persistent button view. The fallback claim remains valid until claimed.
        """
        for trainee_id, pending in DATA["pending"].items():
            if pending.get("state") != "open":
                continue
            self.add_view(
                FallbackClaimView(int(trainee_id)),
                message_id=pending.get("announcement_message_id"),
            )


bot = TrainnexBot()


async def get_training_channel(channel_id: int) -> Optional[discord.TextChannel]:
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    try:
        fetched = await bot.fetch_channel(channel_id)
        return fetched if isinstance(fetched, discord.TextChannel) else None
    except discord.HTTPException:
        return None


async def resolve_user(user_id: int) -> Optional[discord.User]:
    user = bot.get_user(user_id)
    if user:
        return user
    try:
        return await bot.fetch_user(user_id)
    except discord.HTTPException:
        return None


def logo_url() -> Optional[str]:
    """
    Discord embeds cannot use a local file as a thumbnail after restart unless
    the image is hosted. The code therefore attaches the logo to bot messages
    when sending an embed and references it as attachment://trainnex_logo.png.
    """
    return "attachment://trainnex_logo.png"


def build_offer_embed(
    trainee_id: int,
    expires_at: int,
    approval_url: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="🚨 Pending Trainee Offer",
        description=(
            f"{get_user_mention(trainee_id)} has been approved and is ready for training.\n\n"
            f"🔗 [View Approval & Results]({approval_url})\n\n"
            f"⏰ This offer will expire {relative_timestamp(expires_at)}."
        ),
        color=WARNING_COLOR,
    )
    embed.set_thumbnail(url=logo_url())
    embed.set_footer(text="Trainnex • Field Training System")
    return embed


def build_expired_embed(trainee_id: int, approval_url: str) -> discord.Embed:
    embed = discord.Embed(
        title="⏰ Offer Expired",
        description=(
            "You did not claim the trainee in time!\n\n"
            f"The trainee {get_user_mention(trainee_id)} has automatically "
            "been passed to the next trainer."
        ),
        color=ERROR_COLOR,
    )
    embed.add_field(
        name="Approval",
        value=f"[View Approval & Results]({approval_url})",
        inline=False,
    )
    embed.set_thumbnail(url=logo_url())
    embed.set_footer(text="Trainnex • Automatic 30-minute timeout")
    return embed


def build_claimed_dm_embed(trainee_id: int, approval_url: str) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Trainee Claimed",
        description=(
            f"You have claimed {get_user_mention(trainee_id)} for Field Training.\n\n"
            "Please contact the trainee and begin the training process."
        ),
        color=SUCCESS_COLOR,
    )
    embed.add_field(
        name="Application",
        value=f"[View Approval & Results]({approval_url})",
        inline=False,
    )
    embed.set_thumbnail(url=logo_url())
    embed.set_footer(text="Trainnex • Field Training System")
    return embed


def build_fallback_embed(trainee_id: int, approval_url: str) -> discord.Embed:
    embed = discord.Embed(
        title="🚨 Unclaimed Trainee",
        description=(
            f"{get_user_mention(trainee_id)} has been approved and is awaiting "
            "a Field Training Officer.\n\n"
            "Any Field Training Officer may claim this trainee.\n\n"
            f"🔗 [View Approval & Results]({approval_url})"
        ),
        color=WARNING_COLOR,
    )
    embed.set_thumbnail(url=logo_url())
    embed.set_footer(text="Trainnex • Open FTO Claim")
    return embed


def build_fallback_claimed_embed(
    trainee_id: int,
    trainer_id: int,
    approval_url: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Trainee Claimed",
        description=(
            f"{get_user_mention(trainee_id)} has been claimed by "
            f"{get_user_mention(trainer_id)} for Field Training."
        ),
        color=SUCCESS_COLOR,
    )
    embed.add_field(
        name="Application",
        value=f"[View Approval & Results]({approval_url})",
        inline=False,
    )
    embed.set_thumbnail(url=logo_url())
    embed.set_footer(text="Trainnex • Field Training System")
    return embed


def build_claim_notification(
    trainee_id: int,
    trainer_id: int,
    approval_url: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Trainee Claimed",
        description=(
            f"{get_user_mention(trainer_id)} has claimed "
            f"{get_user_mention(trainee_id)} for Field Training."
        ),
        color=SUCCESS_COLOR,
    )
    embed.add_field(
        name="Approval",
        value=f"[View Approval & Results]({approval_url})",
        inline=False,
    )
    embed.set_thumbnail(url=logo_url())
    embed.set_footer(text="Trainnex • Field Training System")
    return embed


class LogoEmbedView(discord.ui.View):
    """Base class for Trainnex button views."""

    def __init__(self, timeout=None):
        super().__init__(timeout=timeout)


class OfferView(LogoEmbedView):
    def __init__(self, offer_id: str):
        super().__init__(timeout=None)
        self.offer_id = offer_id
        # Persistent views need unique custom IDs for each active offer.
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.label == "CLAIM":
                    child.custom_id = f"trainnex:claim:{offer_id}"
                elif child.label == "PASS ON":
                    child.custom_id = f"trainnex:pass:{offer_id}"

    @discord.ui.button(
        label="CLAIM",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="trainnex:claim:placeholder",
    )
    async def claim(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await handle_offer_claim(interaction, self.offer_id)

    @discord.ui.button(
        label="PASS ON",
        style=discord.ButtonStyle.secondary,
        emoji="➡️",
        custom_id="trainnex:pass:placeholder",
    )
    async def pass_on(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await handle_offer_pass(interaction, self.offer_id)


class FallbackClaimView(LogoEmbedView):
    def __init__(self, trainee_id: int):
        super().__init__(timeout=None)
        self.trainee_id = trainee_id

    @discord.ui.button(
        label="CLAIM",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="trainnex:fallback_claim",
    )
    async def claim(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await handle_fallback_claim(interaction, self.trainee_id)


async def edit_dm_offer_as_expired(offer: dict):
    trainer_id = int(offer["trainer_id"])
    trainee_id = int(offer["trainee_id"])

    trainer = await resolve_user(trainer_id)
    if not trainer:
        return

    try:
        dm = trainer.dm_channel or await trainer.create_dm()
        message = await dm.fetch_message(int(offer["dm_message_id"]))

        embed = build_expired_embed(
            trainee_id,
            offer["approval_url"],
        )

        await message.edit(
            embed=embed,
            view=None,
        )
    except discord.HTTPException:
        log.warning("Could not edit expired DM for offer %s", offer["offer_id"])


async def send_offer_to_trainer(
    trainee_id: int,
    trainer_id: int,
    approval_url: str,
) -> bool:
    trainer = await resolve_user(trainer_id)
    if not trainer:
        return False

    expires_at = int(time.time() + OFFER_TIMEOUT_SECONDS)
    offer_id = f"{trainee_id}-{int(time.time() * 1000)}"

    embed = build_offer_embed(
        trainee_id,
        expires_at,
        approval_url,
    )
    view = OfferView(offer_id)

    try:
        log.info("Attempting DM: trainer=%s (%s)", trainer, trainer_id)
        dm = trainer.dm_channel or await trainer.create_dm()
        log.info("DM channel ready: trainer=%s channel=%s", trainer_id, dm.id)
        message = await dm.send(
            embed=embed,
            view=view,
            files=[
                discord.File(
                    "assets/trainnex_logo.png",
                    filename="trainnex_logo.png",
                )
            ],
        )
    except discord.Forbidden as e:
        log.error(
            "DM FORBIDDEN for trainer=%s. Discord blocked the DM. status=%s code=%s text=%s",
            trainer_id, getattr(e, "status", None), getattr(e, "code", None), str(e)
        )
        return False
    except discord.HTTPException as e:
        log.error(
            "DM HTTP ERROR for trainer=%s. status=%s code=%s text=%s",
            trainer_id, getattr(e, "status", None), getattr(e, "code", None), str(e)
        )
        return False
    except Exception:
        log.exception("Unexpected error while DMing trainer=%s", trainer_id)
        return False

    DATA["offers"][offer_id] = {
        "offer_id": offer_id,
        "trainee_id": trainee_id,
        "trainer_id": trainer_id,
        "approval_url": approval_url,
        "dm_message_id": message.id,
        "created_at": time.time(),
        "expires_at": expires_at,
        "state": "pending",
    }

    # Track who has already been offered this trainee.
    pending = DATA["pending"].setdefault(
        str(trainee_id),
        {
            "trainee_id": trainee_id,
            "approval_url": approval_url,
            "state": "routing",
            "attempted_trainers": [],
        },
    )
    pending["attempted_trainers"].append(trainer_id)

    save_data()
    bot.schedule_offer_expiration(offer_id)

    log.info(
        "Offer sent: trainee=%s trainer=%s offer=%s",
        trainee_id,
        trainer_id,
        offer_id,
    )
    return True


async def route_trainee(
    trainee_id: int,
    approval_url: str,
    attempted: Optional[set[int]] = None,
) -> bool:
    """
    Routes a trainee to the next eligible FTO.

    Each FTO gets one opportunity for this trainee. If all FTOs have been
    attempted, the trainee goes to the open FTO fallback announcement.
    """
    attempted = attempted or set()

    # Merge with persistent attempts.
    pending = DATA["pending"].setdefault(
        str(trainee_id),
        {
            "trainee_id": trainee_id,
            "approval_url": approval_url,
            "state": "routing",
            "attempted_trainers": [],
        },
    )

    attempted.update(int(x) for x in pending.get("attempted_trainers", []))

    normalize_queue()

    if not DATA["queue"]:
        await create_fallback_announcement(trainee_id, approval_url)
        return False

    # We allow a busy trainer to be skipped without counting them as an attempt.
    busy = {
        int(offer["trainer_id"])
        for offer in DATA["offers"].values()
        if offer.get("state") == "pending"
    }

    candidates = []
    for trainer_id in DATA["queue"]:
        if trainer_id in attempted:
            continue
        if trainer_id in busy:
            continue
        candidates.append(trainer_id)

    # If all remaining FTOs are busy, wait for one of them to finish rather
    # than assigning the same person multiple simultaneous offers.
    if not candidates:
        unattempted = [
            uid for uid in DATA["queue"]
            if uid not in attempted
        ]
        if unattempted:
            return False

        await create_fallback_announcement(trainee_id, approval_url)
        return False

    for trainer_id in candidates:
        success = await send_offer_to_trainer(
            trainee_id,
            trainer_id,
            approval_url,
        )

        if success:
            return True

        # DM failure counts as an attempt and the FTO gets moved to the back.
        attempted.add(trainer_id)
        pending["attempted_trainers"].append(trainer_id)
        move_to_back(trainer_id)
        save_data()

    await create_fallback_announcement(trainee_id, approval_url)
    return False


async def finish_offer(offer_id: str, outcome: str, interaction=None):
    """
    Atomically-ish finish an offer:
      claim -> trainer gets trainee, moves to back
      pass/timeout -> trainer moves to back and next FTO gets offer
    """
    offer = DATA["offers"].get(offer_id)
    if not offer or offer.get("state") != "pending":
        if interaction and not interaction.response.is_done():
            await interaction.response.send_message(
                "⚠️ This offer is no longer active.",
                ephemeral=True,
            )
        return

    trainee_id = int(offer["trainee_id"])
    trainer_id = int(offer["trainer_id"])
    approval_url = offer["approval_url"]

    offer["state"] = outcome
    offer["finished_at"] = time.time()

    task = bot.expiration_tasks.pop(offer_id, None)
    if task and not task.done():
        task.cancel()

    pending = DATA["pending"].setdefault(
        str(trainee_id),
        {
            "trainee_id": trainee_id,
            "approval_url": approval_url,
            "state": "routing",
            "attempted_trainers": [],
        },
    )

    # The trainer has now had their opportunity.
    attempted = set(int(x) for x in pending.get("attempted_trainers", []))
    attempted.add(trainer_id)
    pending["attempted_trainers"] = sorted(attempted)

    # Every outcome puts the trainer at the back.
    move_to_back(trainer_id)

    if outcome == "claimed":
        pending["state"] = "claimed"
        pending["claimed_by"] = trainer_id
        pending["claimed_at"] = time.time()

        save_data()

        # Update the trainer's DM.
        trainer = await resolve_user(trainer_id)
        if trainer:
            try:
                dm = trainer.dm_channel or await trainer.create_dm()
                dm_message = await dm.fetch_message(int(offer["dm_message_id"]))
                await dm_message.edit(
                    embed=build_claimed_dm_embed(
                        trainee_id,
                        approval_url,
                    ),
                    view=None,
                )
            except discord.HTTPException:
                log.warning("Could not update claimed DM for %s", offer_id)

        if interaction:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "✅ You claimed this trainee.",
                        ephemeral=True,
                    )
            except discord.HTTPException:
                pass

        await send_claim_notification(
            trainee_id,
            trainer_id,
            approval_url,
        )
        return

    # PASS or TIMEOUT
    save_data()

    if interaction:
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "➡️ This trainee has been passed to the next FTO.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass

    await route_trainee(
        trainee_id,
        approval_url,
        attempted=attempted,
    )


async def handle_offer_claim(interaction: discord.Interaction, offer_id: str):
    offer = DATA["offers"].get(offer_id)
    if not offer or offer.get("state") != "pending":
        await interaction.response.send_message(
            "⚠️ This trainee offer is no longer active.",
            ephemeral=True,
        )
        return

    if interaction.user.id != int(offer["trainer_id"]):
        await interaction.response.send_message(
            "⛔ This offer is assigned to another Field Training Officer.",
            ephemeral=True,
        )
        return

    if time.time() >= float(offer["expires_at"]):
        await interaction.response.send_message(
            "⏰ This offer has expired.",
            ephemeral=True,
        )
        await expire_offer(offer_id)
        return

    await finish_offer(offer_id, "claimed", interaction)


async def handle_offer_pass(interaction: discord.Interaction, offer_id: str):
    offer = DATA["offers"].get(offer_id)
    if not offer or offer.get("state") != "pending":
        await interaction.response.send_message(
            "⚠️ This trainee offer is no longer active.",
            ephemeral=True,
        )
        return

    if interaction.user.id != int(offer["trainer_id"]):
        await interaction.response.send_message(
            "⛔ This offer is assigned to another Field Training Officer.",
            ephemeral=True,
        )
        return

    await finish_offer(offer_id, "passed", interaction)


async def expire_offer(offer_id: str):
    offer = DATA["offers"].get(offer_id)
    if not offer or offer.get("state") != "pending":
        return

    # Change state first so the button cannot race the timeout.
    offer["state"] = "expired"
    save_data()

    await edit_dm_offer_as_expired(offer)

    # Reuse finish logic, but the state is already changed. Manually perform
    # the routing portion.
    trainee_id = int(offer["trainee_id"])
    trainer_id = int(offer["trainer_id"])
    approval_url = offer["approval_url"]

    pending = DATA["pending"].setdefault(
        str(trainee_id),
        {
            "trainee_id": trainee_id,
            "approval_url": approval_url,
            "state": "routing",
            "attempted_trainers": [],
        },
    )

    attempted = set(int(x) for x in pending.get("attempted_trainers", []))
    attempted.add(trainer_id)
    pending["attempted_trainers"] = sorted(attempted)

    move_to_back(trainer_id)
    save_data()

    await route_trainee(
        trainee_id,
        approval_url,
        attempted=attempted,
    )


async def create_fallback_announcement(
    trainee_id: int,
    approval_url: str,
):
    # Avoid duplicate fallback messages.
    existing = DATA["pending"].get(str(trainee_id))
    if existing and existing.get("state") == "open":
        return

    channel = await get_training_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel:
        log.error("Could not find announcement channel.")
        return

    guild = bot.get_guild(TRAINING_SERVER_ID)
    if not guild:
        log.error("Could not find training server.")
        return

    content = f"<@&{FTO_ROLE_ID}>"

    embed = build_fallback_embed(
        trainee_id,
        approval_url,
    )

    try:
        message = await channel.send(
            content=content,
            embed=embed,
            view=FallbackClaimView(trainee_id),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
            files=[
                discord.File(
                    "assets/trainnex_logo.png",
                    filename="trainnex_logo.png",
                )
            ],
        )
    except discord.HTTPException:
        log.exception("Failed to send fallback announcement.")
        return

    DATA["pending"][str(trainee_id)] = {
        "trainee_id": trainee_id,
        "approval_url": approval_url,
        "state": "open",
        "announcement_message_id": message.id,
        "announcement_channel_id": channel.id,
        "attempted_trainers": DATA["pending"].get(
            str(trainee_id), {}
        ).get("attempted_trainers", []),
    }
    save_data()

    log.info("Fallback announcement created for trainee %s", trainee_id)


async def handle_fallback_claim(
    interaction: discord.Interaction,
    trainee_id: int,
):
    if interaction.guild is None or interaction.guild.id != TRAINING_SERVER_ID:
        await interaction.response.send_message(
            "⛔ This button can only be used in the training server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "⛔ Could not verify your server membership.",
            ephemeral=True,
        )
        return

    if not has_fto_role(interaction.user):
        await interaction.response.send_message(
            "⛔ You must have the Field Training Officer role to claim this trainee.",
            ephemeral=True,
        )
        return

    pending = DATA["pending"].get(str(trainee_id))
    if not pending or pending.get("state") != "open":
        await interaction.response.send_message(
            "⚠️ This trainee has already been claimed or is no longer available.",
            ephemeral=True,
        )
        return

    # Prevent claiming if the FTO is already handling another offer.
    if any(
        offer.get("trainer_id") == interaction.user.id
        and offer.get("state") == "pending"
        for offer in DATA["offers"].values()
    ):
        await interaction.response.send_message(
            "⚠️ You already have a pending trainee offer.",
            ephemeral=True,
        )
        return

    pending["state"] = "claimed"
    pending["claimed_by"] = interaction.user.id
    pending["claimed_at"] = time.time()

    move_to_back(interaction.user.id)
    save_data()

    approval_url = pending["approval_url"]

    # Edit the original fallback announcement.
    try:
        channel = await get_training_channel(ANNOUNCEMENT_CHANNEL_ID)
        if channel:
            message = await channel.fetch_message(
                int(pending["announcement_message_id"])
            )
            await message.edit(
                content="",
                embed=build_fallback_claimed_embed(
                    trainee_id,
                    interaction.user.id,
                    approval_url,
                ),
                view=None,
            )
    except discord.HTTPException:
        log.warning("Could not edit fallback announcement for trainee %s", trainee_id)

    await interaction.response.send_message(
        "✅ You claimed this trainee.",
        ephemeral=True,
    )

    await send_claim_notification(
        trainee_id,
        interaction.user.id,
        approval_url,
    )


async def send_claim_notification(
    trainee_id: int,
    trainer_id: int,
    approval_url: str,
):
    channel = await get_training_channel(CLAIM_NOTIFICATION_CHANNEL_ID)
    if not channel:
        log.error("Could not find claim notification channel.")
        return

    embed = build_claim_notification(
        trainee_id,
        trainer_id,
        approval_url,
    )

    try:
        await channel.send(
            embed=embed,
            files=[
                discord.File(
                    "assets/trainnex_logo.png",
                    filename="trainnex_logo.png",
                )
            ],
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except discord.HTTPException:
        log.exception("Could not send claim notification.")


async def send_acceptance_dm(trainee_id: int) -> bool:
    """Send the newly accepted trainee their WCSO training information."""
    trainee = await resolve_user(trainee_id)
    if not trainee:
        log.error("Could not find accepted trainee user=%s for acceptance DM.", trainee_id)
        return False

    message = (
        "🎉 **WCSO Application Accepted**\n\n"
        "Congratulations! Your WCSO Department Application has been accepted.\n\n"
        "Please join this server for your WCSO Training period:\n"
        "https://discord.gg/mBw5HNPWnv\n\n"
        "A Field Training Officer will contact you regarding the next steps in your training.\n\n"
        "Welcome to WCSO!

You are now a **probationary deputy**."
    )

    try:
        log.info("Sending acceptance DM: trainee=%s (%s)", trainee, trainee_id)
        dm = trainee.dm_channel or await trainee.create_dm()
        await dm.send(message)
        log.info("Acceptance DM sent: trainee=%s", trainee_id)
        return True
    except discord.Forbidden as e:
        log.error(
            "ACCEPTANCE DM FORBIDDEN for trainee=%s. status=%s code=%s text=%s",
            trainee_id, getattr(e, "status", None), getattr(e, "code", None), str(e),
        )
        return False
    except discord.HTTPException as e:
        log.error(
            "ACCEPTANCE DM HTTP ERROR for trainee=%s. status=%s code=%s text=%s",
            trainee_id, getattr(e, "status", None), getattr(e, "code", None), str(e),
        )
        return False
    except Exception:
        log.exception("Unexpected error while sending acceptance DM to trainee=%s", trainee_id)
        return False


async def process_approval(message: discord.Message):
    application_guild = bot.get_guild(APPLICATION_SERVER_ID)
    if not application_guild:
        log.error("Application server is not available to the bot.")
        return

    embed = message.embeds[0]
    trainee_id = parse_trainee_id(message, application_guild)

    if not trainee_id:
        log.error(
            "Melony approval detected, but no trainee ID could be extracted. "
            "Message ID: %s",
            message.id,
        )
        return

    approval_url = message.jump_url

    # Don't process the same approval twice.
    if message.id in DATA["processed_approval_messages"]:
        return

    DATA["processed_approval_messages"].append(message.id)
    DATA["processed_approval_messages"] = DATA["processed_approval_messages"][-500:]
    DATA["pending"][str(trainee_id)] = {
        "trainee_id": trainee_id,
        "approval_url": approval_url,
        "state": "routing",
        "attempted_trainers": [],
    }
    save_data()

    log.info(
        "New approved trainee: trainee=%s approval=%s",
        trainee_id,
        approval_url,
    )

    # Notify the newly accepted trainee directly.
    await send_acceptance_dm(trainee_id)

    await route_trainee(
        trainee_id,
        approval_url,
        attempted=set(),
    )


@bot.event
async def on_message(message: discord.Message):
    # Diagnostic logging for Melony's configured approval channel. This makes
    # it immediately clear whether Discord delivered the message to Trainnex.
    if (
        message.guild is not None
        and message.guild.id == APPLICATION_SERVER_ID
        and message.channel.id == APPROVAL_CHANNEL_ID
    ):
        embed_dump = []
        for embed in message.embeds:
            try:
                embed_dump.append(embed.to_dict())
            except Exception:
                embed_dump.append({"error": "could not serialize embed"})

        log.info(
            "Application-channel message received: author=%s (%s), embeds=%s, mentions=%s, raw_mentions=%s, content=%r, title=%r, embeds_raw=%r",
            message.author,
            message.author.id,
            len(message.embeds),
            [m.id for m in message.mentions],
            getattr(message, "raw_mentions", []),
            message.content,
            (get_melony_embed_title(message) if message.embeds else None),
            embed_dump,
        )

    if is_melony_approval(message):
        try:
            log.info("Melony Response Approved detected: message=%s", message.id)
            await process_approval(message)
        except Exception:
            log.exception("Error processing Melony approval message.")

    await bot.process_commands(message)


# ============================================================
# Slash command group: /trainner
# ============================================================

trainner = app_commands.Group(
    name="trainner",
    description="Manage the Trainnex FTO queue.",
)


@trainner.command(name="testdm", description="Test whether Trainnex can DM an FTO.")
@app_commands.describe(trainer="The Field Training Officer to DM.")
async def trainner_testdm(
    interaction: discord.Interaction,
    trainer: discord.Member,
):
    if interaction.guild is None or interaction.guild.id != TRAINING_SERVER_ID:
        await interaction.response.send_message(
            "⛔ This command can only be used in the training server.",
            ephemeral=True,
        )
        return
    if not isinstance(interaction.user, discord.Member) or not is_queue_manager(interaction.user):
        await interaction.response.send_message(
            "⛔ You need the FTO Commander or FTO Overseer role to use this test.",
            ephemeral=True,
        )
        return

    try:
        dm = trainer.dm_channel or await trainer.create_dm()
        await dm.send(
            "🧪 **Trainnex DM Test**\n\nIf you received this message, Trainnex can successfully DM you.",
        )
        await interaction.response.send_message(
            f"✅ DM test sent to {trainer.mention}. Check their DMs.",
            ephemeral=True,
        )
        log.info("DM test succeeded: trainer=%s (%s)", trainer, trainer.id)
    except discord.Forbidden as e:
        log.error(
            "DM TEST FORBIDDEN for trainer=%s. status=%s code=%s text=%s",
            trainer.id, getattr(e, "status", None), getattr(e, "code", None), str(e)
        )
        await interaction.response.send_message(
            f"❌ Discord blocked the DM to {trainer.mention}.\n\n"
            "Have that FTO check Discord Privacy Settings and make sure **Direct Messages** are enabled for the server. "
            "Then test again.",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        log.exception("DM test HTTP error for trainer=%s", trainer.id)
        await interaction.response.send_message(
            f"❌ Discord returned an error while sending the DM (status {getattr(e, 'status', 'unknown')}). Check the terminal for details.",
            ephemeral=True,
        )


@trainner.command(name="add", description="Add an FTO to the end of the queue.")
@app_commands.describe(trainer="The Field Training Officer to add.")
async def trainner_add(
    interaction: discord.Interaction,
    trainer: discord.Member,
):
    if interaction.guild is None or interaction.guild.id != TRAINING_SERVER_ID:
        await interaction.response.send_message(
            "⛔ This command can only be used in the training server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not is_queue_manager(interaction.user):
        await interaction.response.send_message(
            "⛔ You need the FTO Commander or FTO Overseer role to manage the queue.",
            ephemeral=True,
        )
        return

    if not has_fto_role(trainer):
        await interaction.response.send_message(
            "⚠️ That member does not have the Field Training Officer role.",
            ephemeral=True,
        )
        return

    if trainer.id in DATA["queue"]:
        await interaction.response.send_message(
            f"⚠️ {trainer.mention} is already in the queue.",
            ephemeral=True,
        )
        return

    DATA["queue"].append(trainer.id)
    save_data()

    await interaction.response.send_message(
        f"✅ Added {trainer.mention} to the end of the Trainnex FTO queue.",
    )


@trainner.command(name="remove", description="Remove an FTO from the queue.")
@app_commands.describe(trainer="The Field Training Officer to remove.")
async def trainner_remove(
    interaction: discord.Interaction,
    trainer: discord.Member,
):
    if interaction.guild is None or interaction.guild.id != TRAINING_SERVER_ID:
        await interaction.response.send_message(
            "⛔ This command can only be used in the training server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not is_queue_manager(interaction.user):
        await interaction.response.send_message(
            "⛔ You need the FTO Commander or FTO Overseer role to manage the queue.",
            ephemeral=True,
        )
        return

    if trainer.id not in DATA["queue"]:
        await interaction.response.send_message(
            f"⚠️ {trainer.mention} is not in the queue.",
            ephemeral=True,
        )
        return

    remove_from_queue(trainer.id)
    save_data()

    await interaction.response.send_message(
        f"✅ Removed {trainer.mention} from the Trainnex FTO queue.",
    )


@trainner.command(name="queue", description="View the current FTO queue.")
async def trainner_queue(interaction: discord.Interaction):
    if interaction.guild is None or interaction.guild.id != TRAINING_SERVER_ID:
        await interaction.response.send_message(
            "⛔ This command can only be used in the training server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "⛔ Could not verify your server membership.",
            ephemeral=True,
        )
        return

    if not (has_fto_role(interaction.user) or is_queue_manager(interaction.user)):
        await interaction.response.send_message(
            "⛔ You must be a Field Training Officer to view the queue.",
            ephemeral=True,
        )
        return

    normalize_queue()

    embed = discord.Embed(
        title="📋 Trainnex FTO Queue",
        color=EMBED_COLOR,
    )
    embed.set_thumbnail(url=logo_url())

    if not DATA["queue"]:
        embed.description = "The FTO queue is currently empty."
    else:
        lines = []
        for index, user_id in enumerate(DATA["queue"], start=1):
            busy = any(
                offer.get("trainer_id") == user_id
                and offer.get("state") == "pending"
                for offer in DATA["offers"].values()
            )
            status = " — 🟡 Pending Offer" if busy else ""
            lines.append(f"**{index}.** <@{user_id}>{status}")
        embed.description = "\n".join(lines)

    embed.set_footer(text=f"{len(DATA['queue'])} FTO(s) in queue.")

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
        files=[
            discord.File(
                "assets/trainnex_logo.png",
                filename="trainnex_logo.png",
            )
        ],
    )


@trainner.command(name="status", description="Show Trainnex's current status.")
async def trainner_status(interaction: discord.Interaction):
    if interaction.guild is None or interaction.guild.id != TRAINING_SERVER_ID:
        await interaction.response.send_message(
            "⛔ This command can only be used in the training server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not is_queue_manager(interaction.user):
        await interaction.response.send_message(
            "⛔ You need the FTO Commander or FTO Overseer role.",
            ephemeral=True,
        )
        return

    active_offers = sum(
        1 for offer in DATA["offers"].values()
        if offer.get("state") == "pending"
    )
    open_fallbacks = sum(
        1 for pending in DATA["pending"].values()
        if pending.get("state") == "open"
    )

    embed = discord.Embed(
        title="Trainnex Status",
        color=EMBED_COLOR,
    )
    embed.set_thumbnail(url=logo_url())
    embed.add_field(name="FTOs in Queue", value=str(len(DATA["queue"])))
    embed.add_field(name="Active Offers", value=str(active_offers))
    embed.add_field(name="Open Fallbacks", value=str(open_fallbacks))
    embed.add_field(
        name="Applications Server",
        value=str(APPLICATION_SERVER_ID),
        inline=False,
    )
    embed.add_field(
        name="Training Server",
        value=str(TRAINING_SERVER_ID),
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
        files=[
            discord.File(
                "assets/trainnex_logo.png",
                filename="trainnex_logo.png",
            )
        ],
    )


@trainner.command(name="reset", description="Clear the FTO queue.")
async def trainner_reset(interaction: discord.Interaction):
    if interaction.guild is None or interaction.guild.id != TRAINING_SERVER_ID:
        await interaction.response.send_message(
            "⛔ This command can only be used in the training server.",
            ephemeral=True,
        )
        return

    if not isinstance(interaction.user, discord.Member) or not is_queue_manager(interaction.user):
        await interaction.response.send_message(
            "⛔ You need the FTO Commander or FTO Overseer role.",
            ephemeral=True,
        )
        return

    DATA["queue"] = []
    save_data()

    await interaction.response.send_message(
        "🧹 The Trainnex FTO queue has been cleared.",
    )


# Add command group to the bot's tree.
bot.tree.add_command(
    trainner,
    guild=discord.Object(id=TRAINING_SERVER_ID),
)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    log.exception("Slash command error: %s", error)

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Trainnex encountered an error while running that command.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Trainnex encountered an error while running that command.",
                ephemeral=True,
            )
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    log.info("Starting Trainnex...")
    bot.run(TOKEN)
