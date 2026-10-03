import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from storage import moderation_db
from common import is_staff, duration_arg


GIVEAWAY_ENTER_PREFIX = "giveaway:enter:"
DEFAULT_TITLE = "🎉 Giveaway 🎉"
DEFAULT_BUTTON_LABEL = "Enter Giveaway"
DEFAULT_EMOJI = "🎉"
DEFAULT_COLOR = discord.Color.green().value
ENDED_COLOR = discord.Color.dark_grey().value

CUSTOM_EMOJI_PATTERN = re.compile(r"^<(a?):(\w+):(\d+)>$")
HEX_COLOR_PATTERN = re.compile(r"^(?:0x|#)?([0-9a-fA-F]{6})$")

ButtonEmoji = Union[str, discord.Emoji, discord.PartialEmoji]

# Columns added after the original release. Keep (name, sql_type, default_sql) in
# migration order so ensure_giveaway_db can add them to existing databases.
GIVEAWAY_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("title", "TEXT", f"'{DEFAULT_TITLE}'"),
    ("description", "TEXT", "NULL"),
    ("button_label", "TEXT", f"'{DEFAULT_BUTTON_LABEL}'"),
    ("emoji", "TEXT", f"'{DEFAULT_EMOJI}'"),
    ("color", "INTEGER", str(DEFAULT_COLOR)),
    ("last_winner_ids", "TEXT", "NULL"),
]


class InvalidEmoji(Exception):
    pass


class InvalidColor(Exception):
    pass


def parse_color(raw: Optional[str]) -> int:
    """
    Resolves a user-supplied accent color for the giveaway embed.

    Accepts a hex color as ``#2ecc71``, ``2ecc71``, or ``0x2ecc71``. Falls
    back to the default green when nothing is supplied.
    """

    if raw is None or not raw.strip():
        return DEFAULT_COLOR

    match = HEX_COLOR_PATTERN.match(raw.strip())

    if not match:
        raise InvalidColor(
            "Invalid color. Use a 6-digit hex color like `#2ecc71`, `2ecc71`, or `0x2ecc71`."
        )

    return int(match.group(1), 16)


def giveaway_db() -> sqlite3.Connection:
    return moderation_db()


def ensure_giveaway_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS giveaways (
            giveaway_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER,
            host_id INTEGER NOT NULL,
            prize TEXT NOT NULL,
            winner_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS giveaway_entries (
            giveaway_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (giveaway_id, user_id)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_giveaways_active
        ON giveaways (completed_at, ends_at)
        """
    )

    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(giveaways)").fetchall()
    }

    for column_name, column_type, default_sql in GIVEAWAY_COLUMN_MIGRATIONS:
        if column_name in existing_columns:
            continue

        conn.execute(
            f"ALTER TABLE giveaways ADD COLUMN {column_name} {column_type} DEFAULT {default_sql}"
        )


def init_giveaway_db() -> None:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)


def parse_emoji(guild: Optional[discord.Guild], raw: Optional[str]) -> tuple[ButtonEmoji, str]:
    """
    Resolves a user-supplied emoji string for use on a button.

    Accepts a raw unicode emoji, a custom emoji mention (``<:name:id>`` /
    ``<a:name:id>``), or a bare custom emoji name available in the guild.
    Returns the resolved emoji object/string plus the canonical text form to
    store for later redisplay.
    """

    if raw is None or not raw.strip():
        return DEFAULT_EMOJI, DEFAULT_EMOJI

    raw = raw.strip()

    match = CUSTOM_EMOJI_PATTERN.match(raw)
    if match:
        animated, name, emoji_id = match.groups()
        partial = discord.PartialEmoji(name=name, id=int(emoji_id), animated=bool(animated))
        return partial, str(partial)

    bare_name = raw.strip(":")

    if guild is not None and bare_name:
        found = discord.utils.find(
            lambda guild_emoji: guild_emoji.name.lower() == bare_name.lower(),
            guild.emojis,
        )

        if found is not None:
            return found, str(found)

    # Not a known custom emoji reference. Treat it as a literal (hopefully
    # unicode) emoji and let Discord validate it when the button is sent.
    if raw.startswith("<") and raw.endswith(">"):
        raise InvalidEmoji(
            "That looks like a custom emoji, but I could not find it. "
            "Make sure the bot is in the server that owns it."
        )

    return raw, raw


def create_giveaway(
    *,
    guild_id: int,
    channel_id: int,
    host_id: int,
    prize: str,
    winner_count: int,
    ends_at: datetime,
    title: str,
    description: Optional[str],
    button_label: str,
    emoji: str,
    color: int,
) -> int:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        cursor = conn.execute(
            """
            INSERT INTO giveaways (
                guild_id, channel_id, host_id, prize, winner_count, created_at, ends_at,
                title, description, button_label, emoji, color
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                host_id,
                prize,
                winner_count,
                created_at,
                ends_at.isoformat(timespec="seconds"),
                title,
                description,
                button_label,
                emoji,
                color,
            ),
        )

        return int(cursor.lastrowid)


def update_giveaway(
    giveaway_id: int,
    *,
    prize: str,
    winner_count: int,
    ends_at: datetime,
    title: str,
    description: Optional[str],
    button_label: str,
    emoji: str,
    color: int,
) -> None:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        conn.execute(
            """
            UPDATE giveaways
            SET prize = ?, winner_count = ?, ends_at = ?, title = ?, description = ?,
                button_label = ?, emoji = ?, color = ?
            WHERE giveaway_id = ?
            """,
            (
                prize,
                winner_count,
                ends_at.isoformat(timespec="seconds"),
                title,
                description,
                button_label,
                emoji,
                color,
                giveaway_id,
            ),
        )


def delete_giveaway(giveaway_id: int) -> None:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        conn.execute("DELETE FROM giveaways WHERE giveaway_id = ?", (giveaway_id,))
        conn.execute("DELETE FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,))


def set_giveaway_message_id(giveaway_id: int, message_id: int) -> None:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        conn.execute(
            "UPDATE giveaways SET message_id = ? WHERE giveaway_id = ?",
            (message_id, giveaway_id),
        )


def set_last_winner_ids(giveaway_id: int, winner_ids: list[int]) -> None:
    value = ",".join(str(winner_id) for winner_id in winner_ids) if winner_ids else None

    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        conn.execute(
            "UPDATE giveaways SET last_winner_ids = ? WHERE giveaway_id = ?",
            (value, giveaway_id),
        )


def parse_winner_ids(row: sqlite3.Row) -> list[int]:
    raw = row["last_winner_ids"]

    if not raw:
        return []

    return [int(value) for value in raw.split(",") if value.strip()]


def get_giveaway(giveaway_id: int) -> Optional[sqlite3.Row]:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        return conn.execute(
            "SELECT * FROM giveaways WHERE giveaway_id = ?",
            (giveaway_id,),
        ).fetchone()


def recent_giveaways_for_guild(guild_id: int, limit: int = 25) -> list[sqlite3.Row]:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        return list(
            conn.execute(
                """
                SELECT * FROM giveaways
                WHERE guild_id = ?
                ORDER BY giveaway_id DESC
                LIMIT ?
                """,
                (guild_id, limit),
            ).fetchall()
        )


def due_giveaways() -> list[sqlite3.Row]:
    now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        return list(
            conn.execute(
                """
                SELECT *
                FROM giveaways
                WHERE completed_at IS NULL
                AND ends_at <= ?
                ORDER BY ends_at ASC
                """,
                (now_text,),
            ).fetchall()
        )


def finish_giveaway(giveaway_id: int) -> None:
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        conn.execute(
            "UPDATE giveaways SET completed_at = ? WHERE giveaway_id = ?",
            (completed_at, giveaway_id),
        )


def add_entry(giveaway_id: int, user_id: int) -> bool:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)",
            (giveaway_id, user_id),
        )

        return cursor.rowcount > 0


def entry_count(giveaway_id: int) -> int:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM giveaway_entries WHERE giveaway_id = ?",
            (giveaway_id,),
        ).fetchone()

        return int(row["count"])


def entrant_ids(giveaway_id: int) -> list[int]:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        rows = conn.execute(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?",
            (giveaway_id,),
        ).fetchall()

        return [int(row["user_id"]) for row in rows]


def giveaway_embed(
    *,
    title: str,
    description: Optional[str],
    prize: str,
    host_id: int,
    winner_count: int,
    ends_at: datetime,
    entries: int,
    color: int = DEFAULT_COLOR,
    ended: bool = False,
    winners: Optional[list[int]] = None,
) -> discord.Embed:
    body = f"**Prize:** {prize}"

    if description:
        body += f"\n\n{description}"

    embed = discord.Embed(
        title=f"{title} (Ended)" if ended else title,
        description=body,
        color=discord.Color(ENDED_COLOR if ended else color),
    )

    embed.add_field(name="Hosted By", value=f"<@{host_id}>", inline=True)
    embed.add_field(name="Winners", value=str(winner_count), inline=True)
    embed.add_field(name="Entries", value=str(entries), inline=True)

    if ended:
        if winners:
            winner_text = ", ".join(f"<@{winner_id}>" for winner_id in winners)
        else:
            winner_text = "No valid entries; no winner could be selected."

        embed.add_field(name="Winner(s)", value=winner_text, inline=False)
    else:
        embed.add_field(
            name="Ends",
            value=discord.utils.format_dt(ends_at, "R"),
            inline=False,
        )
        embed.description += "\n\nClick the button below to enter!"

    return embed


def embed_from_row(row: sqlite3.Row, *, entries: int, ended: bool = False, winners: Optional[list[int]] = None) -> discord.Embed:
    ends_at = datetime.fromisoformat(row["ends_at"])
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)

    color = row["color"]

    return giveaway_embed(
        title=row["title"] or DEFAULT_TITLE,
        description=row["description"],
        prize=row["prize"],
        host_id=int(row["host_id"]),
        winner_count=int(row["winner_count"]),
        ends_at=ends_at,
        entries=entries,
        color=int(color) if color is not None else DEFAULT_COLOR,
        ended=ended,
        winners=winners,
    )


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int, *, button_label: str, emoji: ButtonEmoji):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        self.add_item(GiveawayEnterButton(giveaway_id, button_label=button_label, emoji=emoji))


class GiveawayEnterButton(discord.ui.Button):
    def __init__(self, giveaway_id: int, *, button_label: str, emoji: ButtonEmoji):
        super().__init__(
            label=button_label[:80],
            emoji=emoji,
            style=discord.ButtonStyle.success,
            custom_id=f"{GIVEAWAY_ENTER_PREFIX}{giveaway_id}",
        )
        self.giveaway_id = giveaway_id

    async def callback(self, interaction: discord.Interaction):
        giveaway = get_giveaway(self.giveaway_id)

        if giveaway is None or giveaway["completed_at"] is not None:
            await interaction.response.send_message(
                "This giveaway has already ended.",
                ephemeral=True,
            )
            return

        added = add_entry(self.giveaway_id, interaction.user.id)

        if not added:
            await interaction.response.send_message(
                "You are already entered in this giveaway.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "You're entered! Good luck.",
            ephemeral=True,
        )

        if interaction.message is not None:
            try:
                await interaction.message.edit(
                    embed=embed_from_row(giveaway, entries=entry_count(self.giveaway_id)),
                    view=self.view,
                )
            except discord.HTTPException:
                pass


async def giveaway_id_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[int]]:
    if interaction.guild is None:
        return []

    current = current.strip().lower()
    choices: list[app_commands.Choice[int]] = []

    for row in recent_giveaways_for_guild(interaction.guild.id, limit=25):
        giveaway_id = int(row["giveaway_id"])
        status = "ended" if row["completed_at"] is not None else "active"
        label = f"#{giveaway_id} [{status}] {row['prize']}"[:100]

        if current and current not in str(giveaway_id) and current not in label.lower():
            continue

        choices.append(app_commands.Choice(name=label, value=giveaway_id))

    return choices[:25]


class Giveaway(commands.Cog):
    giveaway_group = app_commands.Group(
        name="giveaway",
        description="Start and manage server giveaways.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_giveaway_db()
        self.giveaway_watcher.start()

    def cog_unload(self):
        self.giveaway_watcher.cancel()

    async def can_run_giveaways(self, interaction: discord.Interaction) -> bool:
        if interaction.user is None:
            return False

        if config.is_bot_owner_id(interaction.user.id):
            return True

        return isinstance(interaction.user, discord.Member) and is_staff(interaction.user)

    async def guard(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.guild.id != config.HOME_GUILD_ID:
            await interaction.response.send_message(
                "This command can only be used in the configured home server.",
                ephemeral=True,
            )
            return False

        if not await self.can_run_giveaways(interaction):
            await interaction.response.send_message(
                "You do not have the required moderator role to use this command.",
                ephemeral=True,
            )
            return False

        return True

    def get_giveaway_in_guild(self, interaction: discord.Interaction, giveaway_id: int) -> Optional[sqlite3.Row]:
        row = get_giveaway(giveaway_id)

        if row is None or interaction.guild is None or int(row["guild_id"]) != interaction.guild.id:
            return None

        return row

    async def fetch_giveaway_channel(self, row: sqlite3.Row) -> Optional[discord.abc.Messageable]:
        channel = self.bot.get_channel(int(row["channel_id"]))

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(row["channel_id"]))
            except discord.HTTPException:
                return None

        return channel

    async def apply_conclusion(
        self,
        row: sqlite3.Row,
        winner_ids: list[int],
        *,
        reroll: bool = False,
    ) -> None:
        giveaway_id = int(row["giveaway_id"])
        set_last_winner_ids(giveaway_id, winner_ids)

        channel = await self.fetch_giveaway_channel(row)
        entries = entry_count(giveaway_id)
        ended_embed = embed_from_row(row, entries=entries, ended=True, winners=winner_ids)

        message_id = row["message_id"]

        if channel is not None and message_id is not None:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(embed=ended_embed, view=None)
            except discord.HTTPException:
                pass

        if channel is None:
            return

        if winner_ids:
            winner_mentions = ", ".join(f"<@{winner_id}>" for winner_id in winner_ids)
            prefix = "🔁 Reroll!" if reroll else "🎉"
            verb = "New winner(s)" if reroll else "Congratulations"
            announcement = f"{prefix} {verb}: {winner_mentions}! You won **{row['prize']}**."
        else:
            announcement = f"No valid entries for the **{row['prize']}** giveaway; no winner was selected."

        try:
            await channel.send(announcement, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass

    @giveaway_group.command(
        name="create",
        description="Start a giveaway that members can enter with a button.",
    )
    @app_commands.describe(
        prize="What is being given away.",
        duration="How long the giveaway runs, e.g. 30s, 10m, 2h/2hr, 7d, 1w.",
        winners="Number of winners to pick. Defaults to 1.",
        channel="Optional channel to post the giveaway in. Defaults to the current channel.",
        title="Optional embed title. Defaults to '🎉 Giveaway 🎉'.",
        description="Optional extra text shown under the prize in the embed.",
        button_label="Optional label for the entry button. Defaults to 'Enter Giveaway'.",
        emoji="Optional emoji for the entry button: a unicode emoji, a custom emoji, or its name. Defaults to 🎉.",
        color="Optional accent color for the embed, as hex (#2ecc71, 2ecc71, or 0x2ecc71). Defaults to green.",
    )
    async def giveaway_create(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        channel: Optional[discord.TextChannel] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        button_label: str = DEFAULT_BUTTON_LABEL,
        emoji: Optional[str] = None,
        color: Optional[str] = None,
    ):
        if not await self.guard(interaction):
            return

        try:
            giveaway_duration = duration_arg(duration, max_duration=timedelta(days=90))
        except commands.BadArgument as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        target_channel = channel or interaction.channel

        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Choose a server text channel.",
                ephemeral=True,
            )
            return

        try:
            resolved_emoji, stored_emoji = parse_emoji(interaction.guild, emoji)
        except InvalidEmoji as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        try:
            resolved_color = parse_color(color)
        except InvalidColor as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        resolved_title = (title or DEFAULT_TITLE)[:256]
        resolved_button_label = (button_label or DEFAULT_BUTTON_LABEL)[:80]

        ends_at = datetime.now(timezone.utc) + giveaway_duration

        giveaway_id = create_giveaway(
            guild_id=interaction.guild.id,
            channel_id=target_channel.id,
            host_id=interaction.user.id,
            prize=prize,
            winner_count=winners,
            ends_at=ends_at,
            title=resolved_title,
            description=description,
            button_label=resolved_button_label,
            emoji=stored_emoji,
            color=resolved_color,
        )

        view = GiveawayView(giveaway_id, button_label=resolved_button_label, emoji=resolved_emoji)

        embed = giveaway_embed(
            title=resolved_title,
            description=description,
            prize=prize,
            host_id=interaction.user.id,
            winner_count=winners,
            ends_at=ends_at,
            entries=0,
            color=resolved_color,
        )

        try:
            message = await target_channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            delete_giveaway(giveaway_id)
            await interaction.response.send_message(
                "I do not have permission to send messages in that channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            delete_giveaway(giveaway_id)
            await interaction.response.send_message(
                f"Could not start that giveaway: `{exc}`",
                ephemeral=True,
            )
            return

        set_giveaway_message_id(giveaway_id, message.id)

        await interaction.response.send_message(
            f"Giveaway `#{giveaway_id}` started in {target_channel.mention}.",
            ephemeral=True,
        )

    @giveaway_group.command(
        name="edit",
        description="Edit an active giveaway's details.",
    )
    @app_commands.describe(
        giveaway_id="The giveaway to edit (see /giveaway create's confirmation, or start typing to search).",
        prize="New prize text.",
        duration="New time remaining from now, e.g. 30s, 10m, 2h/2hr, 7d, 1w.",
        winners="New number of winners to pick.",
        title="New embed title.",
        description="New extra text shown under the prize in the embed.",
        clear_description="Set to true to remove the current description instead of changing it.",
        button_label="New label for the entry button.",
        emoji="New emoji for the entry button: a unicode emoji, a custom emoji, or its name.",
        color="New accent color for the embed, as hex (#2ecc71, 2ecc71, or 0x2ecc71).",
    )
    @app_commands.autocomplete(giveaway_id=giveaway_id_autocomplete)
    async def giveaway_edit(
        self,
        interaction: discord.Interaction,
        giveaway_id: int,
        prize: Optional[str] = None,
        duration: Optional[str] = None,
        winners: Optional[app_commands.Range[int, 1, 20]] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        clear_description: bool = False,
        button_label: Optional[str] = None,
        emoji: Optional[str] = None,
        color: Optional[str] = None,
    ):
        if not await self.guard(interaction):
            return

        row = self.get_giveaway_in_guild(interaction, giveaway_id)

        if row is None:
            await interaction.response.send_message(
                f"No giveaway `#{giveaway_id}` was found in this server.",
                ephemeral=True,
            )
            return

        if row["completed_at"] is not None:
            await interaction.response.send_message(
                f"Giveaway `#{giveaway_id}` has already ended and cannot be edited. "
                "Start a new one instead.",
                ephemeral=True,
            )
            return

        if duration is not None:
            try:
                giveaway_duration = duration_arg(duration, max_duration=timedelta(days=90))
            except commands.BadArgument as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            new_ends_at = datetime.now(timezone.utc) + giveaway_duration
        else:
            new_ends_at = datetime.fromisoformat(row["ends_at"])
            if new_ends_at.tzinfo is None:
                new_ends_at = new_ends_at.replace(tzinfo=timezone.utc)

        if emoji is not None:
            try:
                resolved_emoji, stored_emoji = parse_emoji(interaction.guild, emoji)
            except InvalidEmoji as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        else:
            stored_emoji = row["emoji"] or DEFAULT_EMOJI
            resolved_emoji, stored_emoji = parse_emoji(interaction.guild, stored_emoji)

        if color is not None:
            try:
                resolved_color = parse_color(color)
            except InvalidColor as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        else:
            resolved_color = int(row["color"]) if row["color"] is not None else DEFAULT_COLOR

        new_prize = prize if prize is not None else row["prize"]
        new_winner_count = winners if winners is not None else int(row["winner_count"])
        new_title = (title[:256] if title is not None else (row["title"] or DEFAULT_TITLE))
        new_button_label = (button_label[:80] if button_label is not None else (row["button_label"] or DEFAULT_BUTTON_LABEL))

        if description is not None:
            new_description = description
        elif clear_description:
            new_description = None
        else:
            new_description = row["description"]

        update_giveaway(
            giveaway_id,
            prize=new_prize,
            winner_count=new_winner_count,
            ends_at=new_ends_at,
            title=new_title,
            description=new_description,
            button_label=new_button_label,
            emoji=stored_emoji,
            color=resolved_color,
        )

        updated_row = get_giveaway(giveaway_id)
        channel = await self.fetch_giveaway_channel(updated_row)
        message_id = updated_row["message_id"]
        edit_note = ""

        if channel is not None and message_id is not None:
            try:
                message = await channel.fetch_message(int(message_id))
                new_view = GiveawayView(giveaway_id, button_label=new_button_label, emoji=resolved_emoji)
                new_embed = embed_from_row(updated_row, entries=entry_count(giveaway_id))
                await message.edit(embed=new_embed, view=new_view)
            except discord.HTTPException as exc:
                edit_note = f"\nSaved, but could not update the live message: `{exc}`"
        else:
            edit_note = "\nSaved, but the original giveaway message could not be found to update."

        await interaction.response.send_message(
            f"Giveaway `#{giveaway_id}` updated.{edit_note}",
            ephemeral=True,
        )

    @giveaway_group.command(
        name="end",
        description="End a giveaway early and pick its winner(s) now.",
    )
    @app_commands.describe(
        giveaway_id="The giveaway to end early.",
    )
    @app_commands.autocomplete(giveaway_id=giveaway_id_autocomplete)
    async def giveaway_end(self, interaction: discord.Interaction, giveaway_id: int):
        if not await self.guard(interaction):
            return

        row = self.get_giveaway_in_guild(interaction, giveaway_id)

        if row is None:
            await interaction.response.send_message(
                f"No giveaway `#{giveaway_id}` was found in this server.",
                ephemeral=True,
            )
            return

        if row["completed_at"] is not None:
            await interaction.response.send_message(
                f"Giveaway `#{giveaway_id}` has already ended.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        candidates = entrant_ids(giveaway_id)
        winner_count = int(row["winner_count"])
        winner_ids = random.sample(candidates, k=min(winner_count, len(candidates))) if candidates else []

        finish_giveaway(giveaway_id)
        await self.apply_conclusion(row, winner_ids)

        if winner_ids:
            winner_text = ", ".join(f"<@{winner_id}>" for winner_id in winner_ids)
        else:
            winner_text = "No valid entries; no winner was selected."

        await interaction.followup.send(
            f"Giveaway `#{giveaway_id}` ended early.\nWinner(s): {winner_text}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @giveaway_group.command(
        name="reroll",
        description="Pick new winner(s) for a giveaway that has already ended.",
    )
    @app_commands.describe(
        giveaway_id="The ended giveaway to reroll.",
        winners="Number of new winners to pick. Defaults to the giveaway's original winner count.",
    )
    @app_commands.autocomplete(giveaway_id=giveaway_id_autocomplete)
    async def giveaway_reroll(
        self,
        interaction: discord.Interaction,
        giveaway_id: int,
        winners: Optional[app_commands.Range[int, 1, 20]] = None,
    ):
        if not await self.guard(interaction):
            return

        row = self.get_giveaway_in_guild(interaction, giveaway_id)

        if row is None:
            await interaction.response.send_message(
                f"No giveaway `#{giveaway_id}` was found in this server.",
                ephemeral=True,
            )
            return

        if row["completed_at"] is None:
            await interaction.response.send_message(
                f"Giveaway `#{giveaway_id}` has not ended yet. "
                "Use `/giveaway end` to end it early, then reroll.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        previous_winner_ids = set(parse_winner_ids(row))
        all_candidates = entrant_ids(giveaway_id)
        fresh_candidates = [user_id for user_id in all_candidates if user_id not in previous_winner_ids]
        candidates = fresh_candidates or all_candidates

        winner_count = winners if winners is not None else int(row["winner_count"])
        winner_ids = random.sample(candidates, k=min(winner_count, len(candidates))) if candidates else []

        await self.apply_conclusion(row, winner_ids, reroll=True)

        if winner_ids:
            winner_text = ", ".join(f"<@{winner_id}>" for winner_id in winner_ids)
        else:
            winner_text = "No valid entries; no winner could be selected."

        await interaction.followup.send(
            f"Giveaway `#{giveaway_id}` rerolled.\nNew winner(s): {winner_text}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @giveaway_group.command(
        name="remove",
        description="Delete a giveaway without picking a winner.",
    )
    @app_commands.describe(
        giveaway_id="The giveaway to delete.",
    )
    @app_commands.autocomplete(giveaway_id=giveaway_id_autocomplete)
    async def giveaway_remove(self, interaction: discord.Interaction, giveaway_id: int):
        if not await self.guard(interaction):
            return

        row = self.get_giveaway_in_guild(interaction, giveaway_id)

        if row is None:
            await interaction.response.send_message(
                f"No giveaway `#{giveaway_id}` was found in this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        channel = await self.fetch_giveaway_channel(row)
        message_id = row["message_id"]

        if channel is not None and message_id is not None:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.delete()
            except discord.HTTPException:
                pass

        delete_giveaway(giveaway_id)

        await interaction.followup.send(
            f"Giveaway `#{giveaway_id}` removed. No winners were selected.",
            ephemeral=True,
        )

    @tasks.loop(seconds=30)
    async def giveaway_watcher(self):
        for row in due_giveaways():
            giveaway_id = int(row["giveaway_id"])
            winner_count = int(row["winner_count"])

            candidates = entrant_ids(giveaway_id)
            winner_ids = random.sample(candidates, k=min(winner_count, len(candidates))) if candidates else []

            finish_giveaway(giveaway_id)
            await self.apply_conclusion(row, winner_ids)

    @giveaway_watcher.before_loop
    async def before_giveaway_watcher(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))