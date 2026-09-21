# meow meow ;3 - thweep
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

import config
from storage import moderation_db
from common import is_staff


LockableChannel = Union[discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel]

CHANNEL_ID_PATTERN = re.compile(r"<#(\d+)>|(\d{15,25})")
ROLE_ID_PATTERN = re.compile(r"<@&(\d+)>|(\d{15,25})")


class LockdownError(Exception):
    pass


def lockdown_db() -> sqlite3.Connection:
    return moderation_db()


def ensure_lockdown_db(conn: sqlite3.Connection) -> None:
    conn.execute( # mm my fingers, yay caps lock ;3
        """
        CREATE TABLE IF NOT EXISTS channel_locks (
            lock_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            had_overwrite INTEGER NOT NULL,
            previous_allow INTEGER NOT NULL,
            previous_deny INTEGER NOT NULL,
            locked_by INTEGER NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_channel_locks_active
        ON channel_locks (guild_id, channel_id, active)
        """
    )


def init_lockdown_db() -> None:
    with lockdown_db() as conn:
        ensure_lockdown_db(conn)

# me when copy paste

def active_lock_targets(guild_id: int, channel_id: int) -> set[int]:
    with lockdown_db() as conn:
        ensure_lockdown_db(conn)
        rows = conn.execute(
            """
            SELECT target_id FROM channel_locks
            WHERE guild_id = ? AND channel_id = ? AND active = 1
            """,
            (guild_id, channel_id),
        ).fetchall()

        return {int(row["target_id"]) for row in rows}


def channels_with_active_locks(guild_id: int) -> set[int]:
    with lockdown_db() as conn:
        ensure_lockdown_db(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT channel_id FROM channel_locks
            WHERE guild_id = ? AND active = 1
            """,
            (guild_id,),
        ).fetchall()

        return {int(row["channel_id"]) for row in rows}


def save_lock_state(
    *,
    guild_id: int,
    channel_id: int,
    target_id: int,
    had_overwrite: bool,
    previous_allow: int,
    previous_deny: int,
    locked_by: int,
    reason: str,
) -> None:
    with lockdown_db() as conn:
        ensure_lockdown_db(conn)
        conn.execute(
            """
            INSERT INTO channel_locks (
                guild_id, channel_id, target_id, had_overwrite,
                previous_allow, previous_deny, locked_by, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                target_id,
                1 if had_overwrite else 0,
                previous_allow,
                previous_deny,
                locked_by,
                reason,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )


def active_lock_rows(guild_id: int, channel_id: int) -> list[sqlite3.Row]:
    with lockdown_db() as conn:
        ensure_lockdown_db(conn)
        return list(
            conn.execute(
                """
                SELECT * FROM channel_locks
                WHERE guild_id = ? AND channel_id = ? AND active = 1
                """,
                (guild_id, channel_id),
            ).fetchall()
        )


def deactivate_locks(guild_id: int, channel_id: int) -> None:
    with lockdown_db() as conn:
        ensure_lockdown_db(conn)
        conn.execute(
            """
            UPDATE channel_locks
            SET active = 0
            WHERE guild_id = ? AND channel_id = ? AND active = 1
            """,
            (guild_id, channel_id),
        )


def parse_id_list(text: str, pattern: re.Pattern[str]) -> list[int]:
    ids: list[int] = []

    for match in pattern.finditer(text):
        raw_id = match.group(1) or match.group(2)

        if raw_id is None:
            continue

        parsed = int(raw_id)

        if parsed not in ids:
            ids.append(parsed)

    return ids


def resolve_channels(guild: discord.Guild, text: str) -> list[LockableChannel]:
    channel_ids = parse_id_list(text, CHANNEL_ID_PATTERN)

    if not channel_ids:
        raise LockdownError(
            "Could not find any channel mentions or IDs in that. "
            "Use `#channel` mentions or raw channel IDs, separated by spaces or commas."
        )

    channels: list[LockableChannel] = []
    missing: list[str] = []

    for channel_id in channel_ids:
        channel = guild.get_channel(channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)):
            missing.append(str(channel_id))
            continue

        channels.append(channel)

    if missing:
        raise LockdownError(
            "Could not find these as lockable channels in this server: "
            f"{', '.join(missing)}"
        )

    return channels


def resolve_roles(guild: discord.Guild, text: str) -> list[discord.Role]:
    role_ids = parse_id_list(text, ROLE_ID_PATTERN)

    if not role_ids:
        raise LockdownError(
            "Could not find any role mentions or IDs in that. "
            "Use `@role` mentions or raw role IDs, separated by spaces or commas."
        )

    roles: list[discord.Role] = []
    missing: list[str] = []

    for role_id in role_ids:
        role = guild.get_role(role_id)

        if role is None:
            missing.append(str(role_id))
            continue

        roles.append(role)

    if missing:
        raise LockdownError(f"Could not find these roles in this server: {', '.join(missing)}")

    return roles


def lock_permission_names(channel: LockableChannel) -> list[str]:
    if isinstance(channel, discord.ForumChannel):
        return ["send_messages_in_threads", "create_forum_threads"]

    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return ["connect", "speak"]

    return ["send_messages", "create_public_threads", "create_private_threads"]


class Lockdown(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_lockdown_db()

    def mod_role_ids(self) -> set[int]:
        return set(config.STAFF_ROLE_IDS) | set(config.BAN_STAFF_ROLE_IDS)

    async def can_run(self, interaction: discord.Interaction) -> bool:
        if interaction.user is None:
            return False

        if config.is_bot_owner_id(interaction.user.id):
            return True

        return isinstance(interaction.user, discord.Member) and is_staff(interaction.user)

    async def send_error(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def lock_channel(
        self,
        channel: LockableChannel,
        *,
        moderator: discord.Member,
        whitelist_roles: list[discord.Role],
        reason: str,
    ) -> str:
        guild = channel.guild
        permission_names = lock_permission_names(channel)

        already_locked = active_lock_targets(guild.id, channel.id)

        allow_roles = [
            role
            for role in whitelist_roles
            if role.id not in already_locked
        ]
        allow_role_ids = {role.id for role in whitelist_roles}

        for role_id in self.mod_role_ids():
            if role_id in allow_role_ids or role_id in already_locked:
                continue

            role = guild.get_role(role_id)
            if role is not None:
                allow_roles.append(role)
                allow_role_ids.add(role_id)

        targets_to_lock: list[Union[discord.Role, discord.Object]] = []

        if guild.default_role.id not in already_locked:
            targets_to_lock.append(guild.default_role)

        deny_audit_reason = f"[{config.CASE_TAG}] Lockdown by {moderator} ({moderator.id}): {reason}"

        for target in targets_to_lock:
            existing_overwrite = channel.overwrites_for(target)
            allow_value, deny_value = existing_overwrite.pair()

            new_overwrite = discord.PermissionOverwrite.from_pair(allow_value, deny_value)
            for permission_name in permission_names:
                setattr(new_overwrite, permission_name, False)

            save_lock_state(
                guild_id=guild.id,
                channel_id=channel.id,
                target_id=target.id,
                had_overwrite=(target in channel.overwrites),
                previous_allow=allow_value.value,
                previous_deny=deny_value.value,
                locked_by=moderator.id,
                reason=reason,
            )

            await channel.set_permissions(target, overwrite=new_overwrite, reason=deny_audit_reason)

        for role in allow_roles:
            existing_overwrite = channel.overwrites_for(role)
            allow_value, deny_value = existing_overwrite.pair()

            new_overwrite = discord.PermissionOverwrite.from_pair(allow_value, deny_value)
            for permission_name in permission_names:
                setattr(new_overwrite, permission_name, True)

            save_lock_state(
                guild_id=guild.id,
                channel_id=channel.id,
                target_id=role.id,
                had_overwrite=(role in channel.overwrites),
                previous_allow=allow_value.value,
                previous_deny=deny_value.value,
                locked_by=moderator.id,
                reason=reason,
            )

            await channel.set_permissions(role, overwrite=new_overwrite, reason=deny_audit_reason)

        return f"Locked {channel.mention}."

    async def unlock_channel(self, channel: LockableChannel, *, moderator: discord.Member, reason: str) -> str:
        guild = channel.guild
        rows = active_lock_rows(guild.id, channel.id)

        if not rows:
            return f"{channel.mention} is not currently locked by this bot."

        audit_reason = f"[{config.CASE_TAG}] Lockdown removed by {moderator} ({moderator.id}): {reason}"

        for row in rows:
            target_id = int(row["target_id"])
            target = guild.default_role if target_id == guild.default_role.id else guild.get_role(target_id)

            if target is None:
                continue

            if not bool(row["had_overwrite"]):
                await channel.set_permissions(target, overwrite=None, reason=audit_reason)
                continue

            restored_overwrite = discord.PermissionOverwrite.from_pair(
                discord.Permissions(int(row["previous_allow"])),
                discord.Permissions(int(row["previous_deny"])),
            )
            await channel.set_permissions(target, overwrite=restored_overwrite, reason=audit_reason)

        deactivate_locks(guild.id, channel.id)

        return f"Unlocked {channel.mention}."

    @app_commands.command(
        name="lockdown",
        description="Lock channels so only mods (and any whitelisted roles) can send messages.",
    )
    @app_commands.describe(
        channels="Channels to lock, as #mentions or IDs. Defaults to the current channel.",
        whitelist_roles="Optional roles that should stay able to send messages, as @mentions or IDs.",
        reason="Reason for the lockdown.",
    )
    async def lockdown(
        self,
        interaction: discord.Interaction,
        channels: Optional[str] = None,
        whitelist_roles: Optional[str] = None,
        reason: str = "No reason provided.",
    ):
        if interaction.guild is None or interaction.guild.id != config.HOME_GUILD_ID:
            await self.send_error(interaction, "This command can only be used in the configured home server.")
            return

        if not await self.can_run(interaction):
            await self.send_error(interaction, "You do not have the required moderator role to use this command.")
            return

        guild = interaction.guild

        try:
            target_channels = (
                resolve_channels(guild, channels)
                if channels
                else [interaction.channel]
                if isinstance(
                    interaction.channel,
                    (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel),
                )
                else []
            )
        except LockdownError as exc:
            await self.send_error(interaction, str(exc))
            return

        if not target_channels:
            await self.send_error(interaction, "No lockable channel was specified or found.")
            return

        try:
            extra_whitelist_roles = resolve_roles(guild, whitelist_roles) if whitelist_roles else []
        except LockdownError as exc:
            await self.send_error(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)

        results: list[str] = []

        for channel in target_channels:
            try:
                detail = await self.lock_channel(
                    channel,
                    moderator=interaction.user,
                    whitelist_roles=extra_whitelist_roles,
                    reason=reason,
                )
                results.append(detail)
            except discord.Forbidden:
                results.append(f"Failed to lock {channel.mention}: missing permissions or role hierarchy is too low.")
            except discord.HTTPException as exc:
                results.append(f"Failed to lock {channel.mention}: Discord API error: `{exc}`")

        await interaction.followup.send("\n".join(results), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(
        name="unlock",
        description="Reverse a /lockdown on the given channels.",
    )
    @app_commands.describe(
        channels="Channels to unlock, as #mentions or IDs. Defaults to the current channel.",
        reason="Reason for removing the lockdown.",
    )
    async def unlock(
        self,
        interaction: discord.Interaction,
        channels: Optional[str] = None,
        reason: str = "No reason provided.",
    ):
        if interaction.guild is None or interaction.guild.id != config.HOME_GUILD_ID:
            await self.send_error(interaction, "This command can only be used in the configured home server.")
            return

        if not await self.can_run(interaction):
            await self.send_error(interaction, "You do not have the required moderator role to use this command.")
            return

        guild = interaction.guild

        try:
            target_channels = (
                resolve_channels(guild, channels)
                if channels
                else [interaction.channel]
                if isinstance(
                    interaction.channel,
                    (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel),
                )
                else []
            )
        except LockdownError as exc:
            await self.send_error(interaction, str(exc))
            return

        if not target_channels:
            locked_ids = channels_with_active_locks(guild.id)

            if not locked_ids:
                await self.send_error(interaction, "No channels are currently locked.")
                return

            target_channels = [
                channel
                for channel_id in locked_ids
                if (channel := guild.get_channel(channel_id)) is not None
            ]

        await interaction.response.defer(ephemeral=True)

        results: list[str] = []

        for channel in target_channels:
            try:
                detail = await self.unlock_channel(channel, moderator=interaction.user, reason=reason)
                results.append(detail)
            except discord.Forbidden:
                results.append(f"Failed to unlock {channel.mention}: missing permissions or role hierarchy is too low.")
            except discord.HTTPException as exc:
                results.append(f"Failed to unlock {channel.mention}: Discord API error: `{exc}`")

        await interaction.followup.send("\n".join(results), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Lockdown(bot))