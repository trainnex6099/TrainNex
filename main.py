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
from summit_api import SummitAPI
from summit_store import (
    init_db as init_summit_db,
    get_trainnex_config,
    save_trainnex_config,
    replace_cache,
    get_message_template,

    # Summit Shifts
    get_shift_settings,
    save_shift_settings,
    list_shift_guild_ids,
    list_shift_types,
    get_shift_type,
    get_shift_type_by_name,
    save_shift_type,
    delete_shift_type,
    start_shift,
    get_shift,
    get_last_shift,
    get_shift_user_stats,
    get_active_shift,
    list_active_shifts,
    set_shift_break,
    end_shift,
    shift_leaderboard,

    # Summit LOA
    list_loa_guild_ids,
    get_loa_settings,
    save_loa_settings,
    create_loa_request,
    get_loa,
    get_current_loa,
    list_user_loas,
    list_pending_loas,
    list_active_loas,
    set_loa_request_message,
    approve_loa,
    deny_loa,
    end_loa,
    list_due_loas,
)


# ============================================================
# Summit - Trainnex Trainer Queue Module
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
SUMMIT_API = SummitAPI()

# ----------------------------
# Trainnex configuration
# ----------------------------
# These values are loaded from the shared Summit database instead of being
# typed into this Python file. The dashboard edits the same database.
init_summit_db()

def load_trainnex_runtime_config() -> None:
    global APPLICATION_SERVER_ID, APPROVAL_CHANNEL_ID, MELONY_BOT_ID
    global TRAINING_SERVER_ID, FTO_ROLE_ID, ANNOUNCEMENT_CHANNEL_ID
    global CLAIM_NOTIFICATION_CHANNEL_ID, FTO_COMMANDER_ROLE_ID, FTO_OVERSEER_ROLE_ID

    cfg = get_trainnex_config()
    APPLICATION_SERVER_ID = int(cfg.get("application_server_id") or 0)
    APPROVAL_CHANNEL_ID = int(cfg.get("approval_channel_id") or 0)
    MELONY_BOT_ID = int(cfg.get("melony_bot_id") or 0)
    TRAINING_SERVER_ID = int(cfg.get("training_server_id") or 0)
    FTO_ROLE_ID = int(cfg.get("fto_role_id") or 0)
    ANNOUNCEMENT_CHANNEL_ID = int(cfg.get("announcement_channel_id") or 0)
    CLAIM_NOTIFICATION_CHANNEL_ID = int(cfg.get("claim_notification_channel_id") or 0)
    FTO_COMMANDER_ROLE_ID = int(cfg.get("fto_commander_role_id") or 0)
    FTO_OVERSEER_ROLE_ID = int(cfg.get("fto_overseer_role_id") or 0)

load_trainnex_runtime_config()


def _api_data(payload):
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _pick(mapping: dict, *names, default=""):
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


async def refresh_trainnex_from_portal() -> None:
    if not SUMMIT_API.configured:
        return
    try:
        payload = _api_data(await SUMMIT_API.get("/api/summit/config/trainnex"))
        cfg = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        save_trainnex_config({
            "application_server_id": _pick(cfg, "application_server_id", "applicationServerId"),
            "approval_channel_id": _pick(cfg, "approval_channel_id", "approvalChannelId"),
            "melony_bot_id": _pick(cfg, "melony_bot_id", "melonyBotId"),
            "training_server_id": _pick(cfg, "training_server_id", "trainingServerId"),
            "fto_role_id": _pick(cfg, "fto_role_id", "ftoRoleId"),
            "announcement_channel_id": _pick(
                cfg, "announcement_channel_id", "open_announcement_channel_id",
                "announcementChannelId", "openAnnouncementChannelId"
            ),
            "claim_notification_channel_id": _pick(
                cfg, "claim_notification_channel_id", "claimNotificationChannelId"
            ),
            "fto_commander_role_id": _pick(
                cfg, "fto_commander_role_id", "ftoCommanderRoleId"
            ),
            "fto_overseer_role_id": _pick(
                cfg, "fto_overseer_role_id", "ftoOverseerRoleId"
            ),
        })
        load_trainnex_runtime_config()
        log.info("Loaded Trainnex configuration from WCSO Portal.")
    except Exception as exc:
        log.warning("Could not load Trainnex configuration from portal: %s", exc)


async def refresh_guild_config_from_portal(guild: discord.Guild) -> None:
    if not SUMMIT_API.configured:
        return
    try:
        shift_payload = _api_data(
            await SUMMIT_API.get("/api/summit/config/shifts", guild.id)
        )
        shift_cfg = (
            shift_payload.get("config")
            if isinstance(shift_payload.get("config"), dict)
            else shift_payload.get("settings")
            if isinstance(shift_payload.get("settings"), dict)
            else shift_payload
        )
        save_shift_settings(guild.id, {
            "admin_role_id": _pick(
                shift_cfg, "admin_role_id", "adminRoleId", "manager_role_id", "managerRoleId"
            )
        })

        remote_types = (
            shift_payload.get("shift_types")
            or shift_payload.get("types")
            or shift_payload.get("shiftTypes")
            or []
        )
        if isinstance(remote_types, list):
            local_by_name = {
                row["name"].lower(): row for row in list_shift_types(guild.id)
            }
            remote_names = set()
            for item in remote_types:
                if not isinstance(item, dict):
                    continue
                name = str(_pick(item, "name", "type_name", "typeName")).strip()
                if not name:
                    continue
                remote_names.add(name.lower())
                local = local_by_name.get(name.lower(), {})
                save_shift_type(guild.id, {
                    "id": local.get("id"),
                    "name": name,
                    "on_shift_role_id": _pick(
                        item, "on_shift_role_id", "onShiftRoleId"
                    ),
                    "on_break_role_id": _pick(
                        item, "on_break_role_id", "onBreakRoleId"
                    ),
                    "log_channel_id": _pick(
                        item, "log_channel_id", "shift_log_channel_id",
                        "logChannelId", "shiftLogChannelId"
                    ),
                    "is_default": bool(_pick(
                        item, "is_default", "isDefault", "default", default=False
                    )),
                })
            if remote_names:
                for row in list_shift_types(guild.id):
                    if row["name"].lower() not in remote_names:
                        delete_shift_type(guild.id, row["id"])

        loa_payload = _api_data(
            await SUMMIT_API.get("/api/summit/config/loa", guild.id)
        )
        loa_cfg = (
            loa_payload.get("config")
            if isinstance(loa_payload.get("config"), dict)
            else loa_payload
        )
        save_loa_settings(guild.id, {
            "enabled": bool(_pick(loa_cfg, "enabled", default=True)),
            "request_channel_id": _pick(
                loa_cfg, "request_channel_id", "requestChannelId"
            ),
            "log_channel_id": _pick(
                loa_cfg, "log_channel_id", "logs_channel_id",
                "logChannelId", "logsChannelId"
            ),
            "on_leave_role_id": _pick(
                loa_cfg, "on_leave_role_id", "onLeaveRoleId"
            ),
        })
        log.info("Loaded Shift/LOA configuration for guild %s from portal.", guild.id)
    except Exception as exc:
        log.warning("Could not load portal config for guild %s: %s", guild.id, exc)


async def sync_guild_directory_to_portal(guild: discord.Guild) -> None:
    if not SUMMIT_API.configured:
        return
    roles = [
        {"id": str(role.id), "name": role.name, "position": int(role.position)}
        for role in guild.roles
        if not role.is_default()
    ]
    channels = [
        {
            "id": str(channel.id),
            "name": channel.name,
            "position": int(getattr(channel, "position", 0)),
            "type": str(channel.type),
        }
        for channel in guild.channels
    ]
    try:
        await SUMMIT_API.sync_roles(guild.id, roles)
        replace_cache(guild.id, "role", roles)
    except Exception as exc:
        log.warning("Could not sync Discord roles for guild %s: %s", guild.id, exc)
    try:
        await SUMMIT_API.sync_channels(guild.id, channels)
        replace_cache(guild.id, "channel", channels)
    except Exception as exc:
        log.warning("Could not sync Discord channels for guild %s: %s", guild.id, exc)


async def sync_shift_record_to_portal(record: dict | None) -> None:
    if not record or not SUMMIT_API.configured:
        return
    try:
        await SUMMIT_API.sync_shift_record(record)
    except Exception as exc:
        log.warning("Could not sync shift %s to portal: %s", record.get("shift_id"), exc)


async def sync_loa_record_to_portal(record: dict | None) -> None:
    if not record or not SUMMIT_API.configured:
        return
    try:
        await SUMMIT_API.sync_loa_record(record)
    except Exception as exc:
        log.warning("Could not sync LOA %s to portal: %s", record.get("loa_id"), exc)

# ----------------------------
# Behavior
# ----------------------------
OFFER_TIMEOUT_SECONDS = 30 * 60
DATA_FILE = Path("data.json")
LOG_FILE = Path("summit.log")

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
log = logging.getLogger("Summit.Trainnex")

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
    """Read Melony's approval title, including a fallback from raw embed data."""
    if not message.embeds:
        return ""

    embed = message.embeds[0]
    title = (embed.title or "").strip()
    if title:
        return title

    # Some webhook/application-generated embeds can expose their data oddly.
    try:
        raw = embed.to_dict()
        return str(raw.get("title") or "").strip()
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


class SummitBot(commands.Bot):
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

        # Reload dashboard-managed settings before command sync.
        load_trainnex_runtime_config()
        sync_ids = {x for x in (TRAINING_SERVER_ID, APPLICATION_SERVER_ID) if x}
        sync_ids.update(list_shift_guild_ids())
        sync_ids.update(list_loa_guild_ids())
        for guild_id in sync_ids:
            target_guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=target_guild)
            await self.tree.sync(guild=target_guild)
        if sync_ids:
            log.info("Summit commands synced to %s configured server(s).", len(sync_ids))
        else:
            log.warning("Trainnex is not configured yet. Open the Summit dashboard to finish setup.")

        # Restore persistent LOA approval buttons after a restart.
        for loa in list_pending_loas():
            message_id = str(loa.get("request_message_id") or "").strip()
            if message_id.isdigit():
                self.add_view(
                    LOAApprovalView(loa["loa_id"]),
                    message_id=int(message_id),
                )

        # Automatic LOA expiration/removal runs in the background.
        if not loa_expiration_loop.is_running():
            loa_expiration_loop.start()
        if not summit_portal_sync_loop.is_running():
            summit_portal_sync_loop.start()

        self.setup_complete = True

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)

        if SUMMIT_API.configured:
            await refresh_trainnex_from_portal()
            for guild in self.guilds:
                await sync_guild_directory_to_portal(guild)
                await refresh_guild_config_from_portal(guild)
                target_guild = discord.Object(id=guild.id)
                self.tree.copy_global_to(guild=target_guild)
                try:
                    await self.tree.sync(guild=target_guild)
                except discord.HTTPException as exc:
                    log.warning("Could not sync commands to guild %s: %s", guild.id, exc)
        else:
            log.warning("Summit Portal API is not configured; using local database settings.")

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


bot = SummitBot()


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


def trainnex_message(key: str, default: str, **values) -> str:
    """Load editable Trainnex copy from the Summit dashboard."""
    template = get_message_template(key) or default
    safe_values = {k: str(v) for k, v in values.items()}
    try:
        return template.format(**safe_values)
    except (KeyError, ValueError):
        log.warning("Invalid message template %s; using default.", key)
        return default.format(**safe_values)


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
        description=trainnex_message(
            "trainer_offer",
            "{trainee} has been approved and is ready for training.\n\n"
            "🔗 [View Approval & Results]({approval_url})\n\n"
            "⏰ This offer will expire {expires}.",
            trainee=get_user_mention(trainee_id),
            approval_url=approval_url,
            expires=relative_timestamp(expires_at),
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
        description=trainnex_message(
            "trainer_claimed",
            "You have claimed {trainee} for Field Training.\n\n"
            "Please contact the trainee and begin the training process.",
            trainee=get_user_mention(trainee_id),
            approval_url=approval_url,
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
        description=trainnex_message(
            "fallback_announcement",
            "{trainee} has been approved and is awaiting a Field Training Officer.\n\n"
            "Any Field Training Officer may claim this trainee.\n\n"
            "🔗 [View Approval & Results]({approval_url})",
            trainee=get_user_mention(trainee_id),
            approval_url=approval_url,
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
bot.tree.add_command(trainner)



# ---------------------------------------------------------------------------
# Summit Shifts
# ---------------------------------------------------------------------------
SHIFT_TYPE_NAMES = ("Patrol", "SRT", "TEU", "K9", "Training", "Dispatcher")


def _fmt_duration(seconds: int) -> str:
    """Human duration with only units that actually apply."""
    seconds = max(0, int(seconds or 0))
    units = (
        ("week", 7 * 24 * 60 * 60),
        ("day", 24 * 60 * 60),
        ("hour", 60 * 60),
        ("minute", 60),
        ("second", 1),
    )
    parts = []
    remaining = seconds
    for name, size in units:
        amount, remaining = divmod(remaining, size)
        if amount:
            parts.append(f"{amount} {name}{'' if amount == 1 else 's'}")
    return ", ".join(parts) if parts else "0 seconds"


def _shift_break_total(s: dict) -> int:
    total = int(s.get("break_seconds") or 0)
    if s.get("status") == "break" and s.get("break_started_at"):
        total += max(0, int(time.time()) - int(s["break_started_at"]))
    return total


def _shift_worked(s: dict) -> int:
    now = int(time.time())
    end = int(s.get("ended_at") or now)
    return max(0, end - int(s["started_at"]) - _shift_break_total(s))


def _discord_time(ts: int) -> str:
    return f"<t:{int(ts)}:R> (<t:{int(ts)}:F>)"


def _member_author(member: discord.abc.User) -> tuple[str, str | None]:
    avatar = member.display_avatar.url if getattr(member, "display_avatar", None) else None
    return f"@{member.name}", avatar


def _wcso_brand(embed: discord.Embed, guild: discord.Guild):
    icon = guild.icon.url if guild.icon else None
    embed.set_author(name="Whatcom County Sheriff's Office", icon_url=icon)


def _is_shift_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    cfg = get_shift_settings(member.guild.id)
    role_id = str(cfg.get("admin_role_id") or "")
    return bool(role_id and any(str(role.id) == role_id for role in member.roles))


async def _apply_shift_roles(member: discord.Member, shift_type: dict, state: str):
    on_role = (
        member.guild.get_role(int(shift_type["on_shift_role_id"]))
        if shift_type.get("on_shift_role_id")
        else None
    )
    break_role = (
        member.guild.get_role(int(shift_type["on_break_role_id"]))
        if shift_type.get("on_break_role_id")
        else None
    )
    try:
        if state == "active":
            if break_role and break_role in member.roles:
                await member.remove_roles(break_role, reason="Summit shift resumed")
            if on_role and on_role not in member.roles:
                await member.add_roles(on_role, reason="Summit shift started/resumed")
        elif state == "break":
            if on_role and on_role in member.roles:
                await member.remove_roles(on_role, reason="Summit shift paused")
            if break_role and break_role not in member.roles:
                await member.add_roles(break_role, reason="Summit shift paused")
        else:
            remove = [role for role in (on_role, break_role) if role and role in member.roles]
            if remove:
                await member.remove_roles(*remove, reason="Summit shift ended")
    except discord.Forbidden:
        log.warning("Could not update shift roles for %s", member.id)


async def _shift_log(
    guild: discord.Guild,
    shift: dict,
    event: str,
    member: discord.Member | None = None,
):
    shift_type = get_shift_type(shift["shift_type_id"])
    channel_id = shift_type.get("log_channel_id") if shift_type else None
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return

    color_map = {
        "started": discord.Color.green(),
        "break_started": discord.Color.gold(),
        "break_ended": discord.Color.green(),
        "ended": discord.Color.red(),
        "admin_ended": discord.Color.red(),
    }
    titles = {
        "started": "Shift Started",
        "break_started": "Shift Break Started",
        "break_ended": "Shift Break Ended",
        "ended": "Shift Ended",
        "admin_ended": "Shift Ended",
    }
    embed = discord.Embed(
        title=f"{titles[event]} • {shift['shift_type_name']}",
        color=color_map[event],
    )

    if member:
        author_name, author_icon = _member_author(member)
        embed.set_author(name=author_name, icon_url=author_icon)
    else:
        embed.set_author(name=f"@{shift.get('username') or shift['user_id']}")

    embed.add_field(name="Staff Member", value=f"<@{shift['user_id']}>", inline=False)

    if event == "started":
        embed.add_field(
            name="Time Started",
            value=_discord_time(shift["started_at"]),
            inline=False,
        )
    elif event == "break_started":
        embed.add_field(
            name="Started",
            value=_discord_time(shift["started_at"]),
            inline=False,
        )
        embed.add_field(
            name="Break Started",
            value=_discord_time(shift["break_started_at"]),
            inline=False,
        )
    elif event == "break_ended":
        embed.add_field(
            name="Started",
            value=_discord_time(shift["started_at"]),
            inline=False,
        )
        embed.add_field(
            name="Break Ended",
            value=_discord_time(int(time.time())),
            inline=False,
        )
        embed.add_field(
            name="Total Break Time",
            value=_fmt_duration(_shift_break_total(shift)),
            inline=False,
        )
    else:
        embed.add_field(
            name="Total Time on Shift",
            value=_fmt_duration(_shift_worked(shift)),
            inline=False,
        )
        embed.add_field(
            name="Break Time",
            value=_fmt_duration(_shift_break_total(shift)),
            inline=False,
        )
        if event == "admin_ended" and shift.get("admin_adjustment"):
            embed.add_field(
                name="Admin Reason",
                value=shift["admin_adjustment"],
                inline=False,
            )

    embed.set_footer(text=f"Shift ID: {shift['shift_id']}")
    await channel.send(embed=embed)


def _shift_manage_embed(
    member: discord.Member,
    selected_type: str,
    active_shift: dict | None = None,
) -> discord.Embed:
    stats = get_shift_user_stats(member.guild.id, member.id)
    last = get_last_shift(member.guild.id, member.id)

    embed = discord.Embed(color=discord.Color.blue())
    embed.set_author(name="Shift Management", icon_url=member.display_avatar.url)

    embed.add_field(
        name="All-Time Information",
        value=(
            f"**Shifts:** {stats['shift_count']}\n"
            f"**Total Duration:** {_fmt_duration(stats['total_seconds'])}\n"
            f"**Average Duration:** {_fmt_duration(stats['average_seconds'])}"
        ),
        inline=False,
    )

    if last:
        last_status = last["status"].replace("_", " ").title()
        embed.add_field(
            name="Last Shift Information",
            value=(
                f"**Status:** {last_status}\n"
                f"**Total Time:** {_fmt_duration(_shift_worked(last))}\n"
                f"**Break Time:** {_fmt_duration(_shift_break_total(last))}"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Last Shift Information",
            value="No previous shifts.",
            inline=False,
        )

    if active_shift:
        status = "On Break" if active_shift["status"] == "break" else "On Shift"
        embed.add_field(
            name="Current Shift",
            value=(
                f"**Status:** {status}\n"
                f"**Type:** {active_shift['shift_type_name']}\n"
                f"**Started:** {_discord_time(active_shift['started_at'])}\n"
                f"**Shift ID:** `{active_shift['shift_id']}`"
            ),
            inline=False,
        )
        selected_type = active_shift["shift_type_name"]

    embed.set_footer(text=f"Shift Type • {selected_type}")
    return embed


class ShiftManageView(discord.ui.View):
    def __init__(self, owner_id: int, shift_type_name: str):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.shift_type_name = shift_type_name

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This shift panel belongs to someone else.", ephemeral=True
            )
            return False
        return True

    def sync_state(self, active: dict | None):
        self.start_btn.disabled = active is not None
        self.pause_btn.disabled = active is None
        self.end_btn.disabled = active is None
        self.pause_btn.label = "Resume" if active and active["status"] == "break" else "Pause"

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        shift_type = get_shift_type_by_name(interaction.guild.id, self.shift_type_name)
        if not shift_type:
            return await interaction.followup.send(
                f"The **{self.shift_type_name}** shift type is not configured in Summit yet.",
                ephemeral=True,
            )
        try:
            shift = start_shift(
                interaction.guild.id,
                interaction.user.id,
                interaction.user.display_name,
                interaction.user.top_role.name,
                shift_type["id"],
            )
            await sync_shift_record_to_portal(shift)
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)

        await _apply_shift_roles(interaction.user, shift_type, "active")
        await _shift_log(interaction.guild, shift, "started", interaction.user)
        self.sync_state(shift)
        await interaction.edit_original_response(
            embed=_shift_manage_embed(interaction.user, self.shift_type_name, shift),
            view=self,
        )

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        shift = get_active_shift(interaction.guild.id, interaction.user.id)
        if not shift:
            return await interaction.followup.send(
                "You do not have an active shift.", ephemeral=True
            )
        try:
            if shift["status"] == "break":
                shift = set_shift_break(shift["shift_id"], False)
                event = "break_ended"
            else:
                shift = set_shift_break(shift["shift_id"], True)
                event = "break_started"
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)

        shift_type = get_shift_type(shift["shift_type_id"])
        await _apply_shift_roles(interaction.user, shift_type, shift["status"])
        await _shift_log(interaction.guild, shift, event, interaction.user)
        await sync_shift_record_to_portal(shift)
        self.shift_type_name = shift["shift_type_name"]
        self.sync_state(shift)
        await interaction.edit_original_response(
            embed=_shift_manage_embed(interaction.user, self.shift_type_name, shift),
            view=self,
        )

    @discord.ui.button(label="End", style=discord.ButtonStyle.danger)
    async def end_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        shift = get_active_shift(interaction.guild.id, interaction.user.id)
        if not shift:
            return await interaction.followup.send(
                "You do not have an active shift.", ephemeral=True
            )
        shift_type = get_shift_type(shift["shift_type_id"])
        shift = end_shift(shift["shift_id"])
        await sync_shift_record_to_portal(shift)
        await _apply_shift_roles(interaction.user, shift_type, "ended")
        await _shift_log(interaction.guild, shift, "ended", interaction.user)
        self.shift_type_name = shift["shift_type_name"]
        self.sync_state(None)
        await interaction.edit_original_response(
            embed=_shift_manage_embed(interaction.user, self.shift_type_name, None),
            view=self,
        )


shift = app_commands.Group(name="shift", description="Summit shift management")


@shift.command(name="manage", description="Manage your Summit shift.")
@app_commands.describe(type="The shift type to manage")
@app_commands.choices(
    type=[app_commands.Choice(name=name, value=name) for name in SHIFT_TYPE_NAMES]
)
async def shift_manage(interaction: discord.Interaction, type: app_commands.Choice[str]):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message(
            "Use this command in a server.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    active = get_active_shift(interaction.guild.id, interaction.user.id)
    selected = active["shift_type_name"] if active else type.value
    view = ShiftManageView(interaction.user.id, selected)
    view.sync_state(active)
    await interaction.followup.send(
        embed=_shift_manage_embed(interaction.user, selected, active),
        view=view,
        ephemeral=True,
    )


@shift.command(name="active", description="View staff who are currently on shift.")
async def shift_active(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("Use this command in a server.", ephemeral=True)
    rows = list_active_shifts(interaction.guild.id)
    embed = discord.Embed(title="Active Shifts", color=discord.Color.blue())
    _wcso_brand(embed, interaction.guild)
    if not rows:
        embed.description = "There are no active shifts."
    else:
        embed.description = "\n".join(
            f"**{index}.** <@{row['user_id']}> • **{row['shift_type_name']}**"
            for index, row in enumerate(rows, 1)
        )
    await interaction.response.send_message(embed=embed)


@shift.command(name="leaderboard", description="View the shift-time leaderboard.")
@app_commands.describe(type="Optionally show only one shift type")
@app_commands.choices(
    type=[app_commands.Choice(name=name, value=name) for name in SHIFT_TYPE_NAMES]
)
async def shift_board(
    interaction: discord.Interaction,
    type: app_commands.Choice[str] | None = None,
):
    if interaction.guild is None:
        return await interaction.response.send_message("Use this command in a server.", ephemeral=True)
    selected = type.value if type else None
    rows = shift_leaderboard(interaction.guild.id, limit=25, shift_type_name=selected)
    embed = discord.Embed(title="Shift Leaderboard", color=discord.Color.blue())
    _wcso_brand(embed, interaction.guild)
    if not rows:
        embed.description = "No shift data yet."
    else:
        embed.description = "\n".join(
            f"**{index}.** <@{row['user_id']}> • {_fmt_duration(row['seconds'])}"
            for index, row in enumerate(rows, 1)
        )
    embed.set_footer(
        text=f"Showing {selected} Shifts" if selected else "Showing All Shift Types"
    )
    await interaction.response.send_message(embed=embed)


@shift.command(name="admin", description="Force-end an active shift by Shift ID.")
@app_commands.describe(shift_id="The Summit Shift ID", reason="Why the shift is being force-ended")
async def shift_admin(
    interaction: discord.Interaction,
    shift_id: str,
    reason: str = "Admin correction",
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in a server.", ephemeral=True)
    if not _is_shift_admin(interaction.user):
        return await interaction.response.send_message(
            "You do not have permission to manage other users' shifts.", ephemeral=True
        )
    shift_record = get_shift(shift_id.upper())
    if (
        not shift_record
        or str(shift_record["guild_id"]) != str(interaction.guild.id)
        or shift_record["status"] not in ("active", "break")
    ):
        return await interaction.response.send_message(
            "That active Shift ID was not found in this server.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    member = interaction.guild.get_member(int(shift_record["user_id"]))
    shift_type = get_shift_type(shift_record["shift_type_id"])
    shift_record = end_shift(
        shift_record["shift_id"], reason, str(interaction.user.id)
    )
    await sync_shift_record_to_portal(shift_record)
    if member:
        await _apply_shift_roles(member, shift_type, "ended")
    await _shift_log(interaction.guild, shift_record, "admin_ended", member)
    await interaction.followup.send(
        f"Ended `{shift_record['shift_id']}` for <@{shift_record['user_id']}>. Reason: {reason}",
        ephemeral=True,
    )


bot.tree.add_command(shift)


# ---------------------------------------------------------------------------
# Summit Leave of Absence
# ---------------------------------------------------------------------------
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([WDH])\s*$", re.IGNORECASE)


def _parse_loa_duration(value: str) -> tuple[str, int]:
    match = _DURATION_RE.fullmatch(value or "")
    if not match:
        raise ValueError("Use a duration like `2W`, `4D`, or `6H`.")
    amount = int(match.group(1))
    unit = match.group(2).upper()
    if amount <= 0:
        raise ValueError("Duration must be greater than zero.")
    multiplier = {"W": 7 * 24 * 3600, "D": 24 * 3600, "H": 3600}[unit]
    return f"{amount}{unit}", amount * multiplier


def _loa_status_label(status: str) -> str:
    return {
        "pending": "Pending",
        "approved": "Approved",
        "denied": "Denied",
        "ended": "Ended",
    }.get(status, status.title())


def _loa_user_author(embed: discord.Embed, user: discord.abc.User):
    embed.set_author(name=f"@{user.name}", icon_url=user.display_avatar.url)


def _loa_manage_embed(member: discord.Member) -> discord.Embed:
    history = list_user_loas(member.guild.id, member.id, limit=5)
    embed = discord.Embed(title="Manage Leave of Absences", color=discord.Color.blue())
    _loa_user_author(embed, member)
    if history:
        lines = []
        for index, loa in enumerate(history, 1):
            duration = _fmt_duration(int(loa["duration_seconds"]))
            lines.append(
                f"**{index}.** <t:{loa['start_at']}:f> [{duration}] • {_loa_status_label(loa['status'])}"
            )
        embed.add_field(name="History", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="History", value="No leave of absence history.", inline=False)
    return embed


def _loa_history_embed(member: discord.Member) -> discord.Embed:
    history = list_user_loas(member.guild.id, member.id, limit=25)
    embed = discord.Embed(title="Leave of Absence History", color=discord.Color.blue())
    _loa_user_author(embed, member)
    if not history:
        embed.description = "No leave of absence history."
        return embed
    for loa in history[:25]:
        embed.add_field(
            name=loa["loa_id"],
            value=(
                f"**Start:** {_discord_time(loa['start_at'])}\n"
                f"**End:** {_discord_time(loa['end_at'])}\n"
                f"**Reason:** {loa['reason']}"
            ),
            inline=False,
        )
    return embed


def _loa_request_embed(guild: discord.Guild, loa: dict, user: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="Leave of Absence Request", color=discord.Color.gold())
    _loa_user_author(embed, user)
    previous = max(0, len(list_user_loas(guild.id, int(loa["user_id"]), limit=100)) - 1)
    embed.add_field(name="Reason", value=loa["reason"], inline=False)
    embed.add_field(name="Duration", value=_fmt_duration(loa["duration_seconds"]), inline=False)
    embed.add_field(name="Previous Leave of Absences", value=str(previous), inline=False)
    embed.add_field(name="Start", value=_discord_time(loa["start_at"]), inline=False)
    embed.add_field(name="Expected End", value=_discord_time(loa["end_at"]), inline=False)
    embed.set_footer(text=f"ID: {loa['loa_id']}")
    return embed


def _loa_reviewed_request_embed(
    guild: discord.Guild,
    loa: dict,
    requester: discord.abc.User,
) -> discord.Embed:
    status = loa["status"]
    color = discord.Color.green() if status == "approved" else discord.Color.red()
    title = "Leave of Absence Approved" if status == "approved" else "Leave of Absence Denied"
    embed = discord.Embed(title=title, color=color)
    if requester:
        _loa_user_author(embed, requester)
    else:
        embed.set_author(name=f"@{loa.get('username') or loa['user_id']}")
    embed.add_field(name="Reason", value=loa["reason"], inline=False)
    embed.add_field(name="Start", value=_discord_time(loa["start_at"]), inline=False)
    embed.add_field(name="End", value=_discord_time(loa["end_at"]), inline=False)
    embed.add_field(name="Reviewed By", value=f"<@{loa['reviewed_by']}>", inline=False)
    if status == "denied":
        embed.add_field(name="Denial Reason", value=loa["denial_reason"], inline=False)
    embed.set_footer(text=f"ID: {loa['loa_id']}")
    return embed


def _loa_log_embed(guild: discord.Guild, loa: dict, event: str) -> discord.Embed:
    color = discord.Color.green()
    title = "Leave of Absence Approved"
    if event == "denied":
        color = discord.Color.red()
        title = "Leave of Absence Denied"
    elif event == "ended":
        color = discord.Color.blue()
        title = "Leave of Absence Ended"
    elif event == "ended_early":
        color = discord.Color.orange()
        title = "Leave of Absence Ended Early"

    embed = discord.Embed(title=title, color=color)
    user = guild.get_member(int(loa["user_id"]))
    if user:
        _loa_user_author(embed, user)
    else:
        embed.set_author(name=f"@{loa.get('username') or loa['user_id']}")

    embed.add_field(name="Start", value=f"<t:{loa['start_at']}:f>", inline=False)
    embed.add_field(name="End", value=f"<t:{loa['end_at']}:f>", inline=False)
    embed.add_field(name="Reason", value=loa["reason"], inline=False)
    if event == "denied":
        embed.add_field(name="Denial Reason", value=loa["denial_reason"], inline=False)
        embed.add_field(name="Denied By", value=f"<@{loa['reviewed_by']}>", inline=False)
    else:
        if loa.get("reviewed_by"):
            embed.add_field(name="Approved By", value=f"<@{loa['reviewed_by']}>", inline=False)
        if event in ("ended", "ended_early"):
            embed.add_field(
                name="Actually Ended",
                value=f"<t:{loa['ended_at']}:f>",
                inline=False,
            )
            ended_by = loa.get("ended_by") or "system"
            embed.add_field(
                name="Ended By",
                value=("Summit (Automatic)" if ended_by == "system" else f"<@{ended_by}>") ,
                inline=False,
            )
    embed.set_footer(text=f"ID: {loa['loa_id']}")
    return embed


async def _send_loa_dm(user: discord.abc.User, guild: discord.Guild, loa: dict, status: str):
    try:
        if status == "pending":
            embed = discord.Embed(
                title="Leave of Absence Pending",
                description=(
                    "Your leave of absence has been submitted to management for approval.\n"
                    f"If approved, it will end at approximately <t:{loa['end_at']}:F> (<t:{loa['end_at']}:R>).\n"
                    f"To manage your leave of absence, run `/loa manage` in **{guild.name}**."
                ),
                color=discord.Color.gold(),
            )
        elif status == "approved":
            embed = discord.Embed(
                title="Leave of Absence Approved",
                description=(
                    f"Your leave of absence is set to end at approximately <t:{loa['end_at']}:F> "
                    f"(<t:{loa['end_at']}:R>).\n"
                    f"To manage your leave of absence, run `/loa manage` in **{guild.name}**."
                ),
                color=discord.Color.green(),
            )
        elif status == "denied":
            embed = discord.Embed(
                title="Leave of Absence Denied",
                description=(
                    f"If you believe this was a mistake, contact **{guild.name}** management.\n\n"
                    f"**Reason**\n{loa['denial_reason']}"
                ),
                color=discord.Color.red(),
            )
        else:
            embed = discord.Embed(
                title="Leave of Absence Ended",
                description="Your leave of absence has ended.",
                color=discord.Color.blue(),
            )
        _wcso_brand(embed, guild)
        embed.set_footer(text=f"ID: {loa['loa_id']}")
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        log.info("Could not DM LOA update to user %s", user.id)


async def _loa_log(guild: discord.Guild, loa: dict, event: str):
    settings = get_loa_settings(guild.id)
    channel_id = str(settings.get("log_channel_id") or "")
    channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
    if isinstance(channel, discord.TextChannel):
        await channel.send(embed=_loa_log_embed(guild, loa, event))


async def _edit_loa_request_message(
    guild: discord.Guild,
    loa: dict,
    requester: discord.abc.User | None,
):
    channel_id = str(loa.get("request_channel_id") or "")
    message_id = str(loa.get("request_message_id") or "")
    if not (channel_id.isdigit() and message_id.isdigit()):
        return
    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(
            embed=_loa_reviewed_request_embed(guild, loa, requester),
            view=None,
        )
    except discord.HTTPException:
        log.warning("Could not edit LOA request message for %s", loa["loa_id"])


def _can_review_loa(member: discord.Member) -> bool:
    settings = get_loa_settings(member.guild.id)
    channel_id = str(settings.get("log_channel_id") or "")
    if not channel_id.isdigit():
        return False
    channel = member.guild.get_channel(int(channel_id))
    return isinstance(channel, discord.TextChannel) and channel.permissions_for(member).view_channel


async def _apply_loa_role(guild: discord.Guild, user_id: int, add: bool):
    settings = get_loa_settings(guild.id)
    role_id = str(settings.get("on_leave_role_id") or "")
    if not role_id.isdigit():
        return
    role = guild.get_role(int(role_id))
    member = guild.get_member(int(user_id))
    if not role or not member:
        return
    try:
        if add and role not in member.roles:
            await member.add_roles(role, reason="Summit LOA approved")
        elif not add and role in member.roles:
            await member.remove_roles(role, reason="Summit LOA ended")
    except discord.Forbidden:
        log.warning("Could not update LOA role for %s", user_id)


class DenyLOAModal(discord.ui.Modal, title="Deny Leave of Absence"):
    reason = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, loa_id: str):
        super().__init__()
        self.loa_id = loa_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not _can_review_loa(interaction.user):
            return await interaction.response.send_message(
                "You do not have permission to review LOA requests.", ephemeral=True
            )
        try:
            loa = deny_loa(self.loa_id, interaction.user.id, str(self.reason))
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)

        await sync_loa_record_to_portal(loa)
        requester = interaction.guild.get_member(int(loa["user_id"])) or await resolve_user(int(loa["user_id"]))
        await _loa_log(interaction.guild, loa, "denied")
        if requester:
            await _send_loa_dm(requester, interaction.guild, loa, "denied")

        await _edit_loa_request_message(interaction.guild, loa, requester)

        confirm = discord.Embed(
            title="Leave of Absence Denied",
            description=(
                "Successfully denied this leave of absence request.\n"
                f"**Reason:** {loa['denial_reason']}"
            ),
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=confirm, ephemeral=True)


class LOAApprovalView(discord.ui.View):
    def __init__(self, loa_id: str):
        super().__init__(timeout=None)
        self.loa_id = loa_id
        self.approve.custom_id = f"summit:loa:approve:{loa_id}"
        self.deny.custom_id = f"summit:loa:deny:{loa_id}"

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="summit:loa:approve:placeholder")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not _can_review_loa(interaction.user):
            return await interaction.response.send_message(
                "You do not have permission to review LOA requests.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        try:
            loa = approve_loa(self.loa_id, interaction.user.id)
        except ValueError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)

        await sync_loa_record_to_portal(loa)
        await _apply_loa_role(interaction.guild, int(loa["user_id"]), True)
        requester = interaction.guild.get_member(int(loa["user_id"])) or await resolve_user(int(loa["user_id"]))
        await _loa_log(interaction.guild, loa, "approved")
        if requester:
            await _send_loa_dm(requester, interaction.guild, loa, "approved")

        await _edit_loa_request_message(interaction.guild, loa, requester)
        await interaction.followup.send("Leave of absence approved.", ephemeral=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="summit:loa:deny:placeholder")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not _can_review_loa(interaction.user):
            return await interaction.response.send_message(
                "You do not have permission to review LOA requests.", ephemeral=True
            )
        await interaction.response.send_modal(DenyLOAModal(self.loa_id))


class CreateLOAModal(discord.ui.Modal, title="Create Leave of Absence"):
    duration = discord.ui.TextInput(
        label="Duration",
        placeholder="Examples: 2W, 4D, 6H",
        required=True,
        max_length=10,
    )
    reason = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        settings = get_loa_settings(interaction.guild.id)
        if not settings or not int(settings.get("enabled") or 0):
            return await interaction.response.send_message(
                "The LOA system is not enabled for this server.", ephemeral=True
            )
        try:
            duration_text, duration_seconds = _parse_loa_duration(str(self.duration))
            loa = create_loa_request(
                interaction.guild.id,
                interaction.user.id,
                interaction.user.display_name,
                duration_text,
                duration_seconds,
                str(self.reason),
            )
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)

        request_channel_id = str(settings.get("request_channel_id") or "")
        request_channel = (
            interaction.guild.get_channel(int(request_channel_id))
            if request_channel_id.isdigit()
            else None
        )
        if not isinstance(request_channel, discord.TextChannel):
            return await interaction.response.send_message(
                "The LOA request channel is not configured correctly.", ephemeral=True
            )

        request_message = await request_channel.send(
            content=f"{interaction.user.mention} has requested a leave of absence.",
            embed=_loa_request_embed(interaction.guild, loa, interaction.user),
            view=LOAApprovalView(loa["loa_id"]),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        loa = set_loa_request_message(loa["loa_id"], request_channel.id, request_message.id)
        await sync_loa_record_to_portal(loa)
        await _send_loa_dm(interaction.user, interaction.guild, loa, "pending")

        confirm = discord.Embed(
            title="Leave of Absence Pending",
            description=(
                "Your leave of absence has been submitted to management for approval.\n"
                f"If approved, it will end at approximately <t:{loa['end_at']}:F> (<t:{loa['end_at']}:R>)."
            ),
            color=discord.Color.gold(),
        )
        _wcso_brand(confirm, interaction.guild)
        confirm.set_footer(text=f"ID: {loa['loa_id']}")
        await interaction.response.send_message(embed=confirm, ephemeral=True)


class LOAManageView(discord.ui.View):
    def __init__(self, owner_id: int, has_current: bool, active_approved: bool):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.start.disabled = has_current
        self.end_early.disabled = not active_approved

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This LOA panel belongs to someone else.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CreateLOAModal())

    @discord.ui.button(label="View Extended History", style=discord.ButtonStyle.secondary)
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=_loa_history_embed(interaction.user), ephemeral=True
        )

    @discord.ui.button(label="End Early", style=discord.ButtonStyle.danger)
    async def end_early(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        current = get_current_loa(interaction.guild.id, interaction.user.id)
        if not current or current["status"] != "approved":
            return await interaction.followup.send(
                "You do not have an active approved leave of absence.", ephemeral=True
            )
        loa = end_loa(current["loa_id"], str(interaction.user.id))
        await sync_loa_record_to_portal(loa)
        await _apply_loa_role(interaction.guild, interaction.user.id, False)
        await _loa_log(interaction.guild, loa, "ended_early")
        await _send_loa_dm(interaction.user, interaction.guild, loa, "ended")
        await interaction.edit_original_response(
            embed=_loa_manage_embed(interaction.user),
            view=LOAManageView(interaction.user.id, False, False),
        )


loa = app_commands.Group(name="loa", description="Summit leave of absence management")


@loa.command(name="manage", description="Manage your leave of absences.")
async def loa_manage(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use this command in a server.", ephemeral=True)
    settings = get_loa_settings(interaction.guild.id)
    if not settings or not int(settings.get("enabled") or 0):
        return await interaction.response.send_message(
            "The LOA system is not enabled for this server.", ephemeral=True
        )
    current = get_current_loa(interaction.guild.id, interaction.user.id)
    has_active = bool(current and current["status"] == "approved")
    await interaction.response.send_message(
        embed=_loa_manage_embed(interaction.user),
        view=LOAManageView(interaction.user.id, bool(current), has_active),
        ephemeral=True,
    )


@loa.command(name="active", description="View active leave of absences.")
async def loa_active(interaction: discord.Interaction):
    if interaction.guild is None:
        return await interaction.response.send_message("Use this command in a server.", ephemeral=True)
    rows = list_active_loas(interaction.guild.id)
    embed = discord.Embed(title="Active Leave of Absences", color=discord.Color.blue())
    _wcso_brand(embed, interaction.guild)
    if not rows:
        embed.description = "There are no active leave of absences."
    else:
        embed.description = "\n".join(
            f"**{i}.** <@{row['user_id']}> • <t:{row['start_at']}:d> → <t:{row['end_at']}:d>"
            for i, row in enumerate(rows, 1)
        )
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(loa)


@tasks.loop(seconds=60)
async def loa_expiration_loop():
    for record in list_due_loas():
        guild = bot.get_guild(int(record["guild_id"]))
        if not guild:
            continue
        try:
            loa_record = end_loa(record["loa_id"], "system", record["end_at"])
        except ValueError:
            continue
        await sync_loa_record_to_portal(loa_record)
        await _apply_loa_role(guild, int(loa_record["user_id"]), False)
        await _loa_log(guild, loa_record, "ended")
        user = guild.get_member(int(loa_record["user_id"])) or await resolve_user(int(loa_record["user_id"]))
        if user:
            await _send_loa_dm(user, guild, loa_record, "ended")


@loa_expiration_loop.before_loop
async def before_loa_expiration_loop():
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def summit_portal_sync_loop():
    if not SUMMIT_API.configured:
        return
    await refresh_trainnex_from_portal()
    for guild in bot.guilds:
        await sync_guild_directory_to_portal(guild)
        await refresh_guild_config_from_portal(guild)


@summit_portal_sync_loop.before_loop
async def before_summit_portal_sync_loop():
    await bot.wait_until_ready()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    log.exception("Slash command error: %s", error)

    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Summit encountered an error while running that command.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Summit encountered an error while running that command.",
                ephemeral=True,
            )
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    log.info("Starting Summit...")
    bot.run(TOKEN)
