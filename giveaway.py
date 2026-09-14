import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from storage import moderation_db
from common import is_staff, duration_arg


GIVEAWAY_ENTER_PREFIX = "giveaway:enter:"


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


def init_giveaway_db() -> None:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)


def create_giveaway(
    *,
    guild_id: int,
    channel_id: int,
    host_id: int,
    prize: str,
    winner_count: int,
    ends_at: datetime,
) -> int:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        cursor = conn.execute(
            """
            INSERT INTO giveaways (
                guild_id, channel_id, host_id, prize, winner_count, created_at, ends_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                host_id,
                prize,
                winner_count,
                created_at,
                ends_at.isoformat(timespec="seconds"),
            ),
        )

        return int(cursor.lastrowid)


def set_giveaway_message_id(giveaway_id: int, message_id: int) -> None:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        conn.execute(
            "UPDATE giveaways SET message_id = ? WHERE giveaway_id = ?",
            (message_id, giveaway_id),
        )


def get_giveaway(giveaway_id: int) -> Optional[sqlite3.Row]:
    with giveaway_db() as conn:
        ensure_giveaway_db(conn)
        return conn.execute(
            "SELECT * FROM giveaways WHERE giveaway_id = ?",
            (giveaway_id,),
        ).fetchone()


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
    prize: str,
    host_id: int,
    winner_count: int,
    ends_at: datetime,
    entries: int,
    ended: bool = False,
    winners: Optional[list[int]] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="Giveaway Ended" if ended else "🎉 Giveaway 🎉",
        description=f"**Prize:** {prize}",
        color=discord.Color.green() if not ended else discord.Color.dark_grey(),
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


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        self.add_item(GiveawayEnterButton(giveaway_id))


class GiveawayEnterButton(discord.ui.Button):
    def __init__(self, giveaway_id: int):
        super().__init__(
            label="Enter Giveaway",
            emoji="🎉",
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
                ends_at = datetime.fromisoformat(giveaway["ends_at"])
                if ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=timezone.utc)

                await interaction.message.edit(
                    embed=giveaway_embed(
                        prize=giveaway["prize"],
                        host_id=giveaway["host_id"],
                        winner_count=giveaway["winner_count"],
                        ends_at=ends_at,
                        entries=entry_count(self.giveaway_id),
                    ),
                    view=self.view,
                )
            except discord.HTTPException:
                pass


class Giveaway(commands.Cog):
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

    @app_commands.command(
        name="giveaway",
        description="Start a giveaway that members can enter with a button.",
    )
    @app_commands.describe(
        prize="What is being given away.",
        duration="How long the giveaway runs, e.g. 30s, 10m, 2h/2hr, 7d, 1w.",
        winners="Number of winners to pick. Defaults to 1.",
        channel="Optional channel to post the giveaway in. Defaults to the current channel.",
    )
    async def giveaway(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: app_commands.Range[int, 1, 20] = 1,
        channel: Optional[discord.TextChannel] = None,
    ):
        if interaction.guild is None or interaction.guild.id != config.HOME_GUILD_ID:
            await interaction.response.send_message(
                "This command can only be used in the configured home server.",
                ephemeral=True,
            )
            return

        if not await self.can_run_giveaways(interaction):
            await interaction.response.send_message(
                "You do not have the required moderator role to use this command.",
                ephemeral=True,
            )
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

        ends_at = datetime.now(timezone.utc) + giveaway_duration

        giveaway_id = create_giveaway(
            guild_id=interaction.guild.id,
            channel_id=target_channel.id,
            host_id=interaction.user.id,
            prize=prize,
            winner_count=winners,
            ends_at=ends_at,
        )

        embed = giveaway_embed(
            prize=prize,
            host_id=interaction.user.id,
            winner_count=winners,
            ends_at=ends_at,
            entries=0,
        )

        view = GiveawayView(giveaway_id)

        try:
            message = await target_channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to send messages in that channel.",
                ephemeral=True,
            )
            return

        set_giveaway_message_id(giveaway_id, message.id)

        await interaction.response.send_message(
            f"Giveaway `#{giveaway_id}` started in {target_channel.mention}.",
            ephemeral=True,
        )

    @tasks.loop(seconds=30)
    async def giveaway_watcher(self):
        for giveaway in due_giveaways():
            giveaway_id = int(giveaway["giveaway_id"])
            winner_count = int(giveaway["winner_count"])

            candidates = entrant_ids(giveaway_id)
            winner_ids = random.sample(candidates, k=min(winner_count, len(candidates))) if candidates else []

            finish_giveaway(giveaway_id)

            channel = self.bot.get_channel(int(giveaway["channel_id"]))

            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(giveaway["channel_id"]))
                except discord.HTTPException:
                    continue

            ends_at = datetime.fromisoformat(giveaway["ends_at"])
            if ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=timezone.utc)

            ended_embed = giveaway_embed(
                prize=giveaway["prize"],
                host_id=int(giveaway["host_id"]),
                winner_count=winner_count,
                ends_at=ends_at,
                entries=len(candidates),
                ended=True,
                winners=winner_ids,
            )

            message_id = giveaway["message_id"]

            if message_id is not None:
                try:
                    message = await channel.fetch_message(int(message_id))
                    await message.edit(embed=ended_embed, view=None)
                except discord.HTTPException:
                    pass

            if winner_ids:
                winner_mentions = ", ".join(f"<@{winner_id}>" for winner_id in winner_ids)
                announcement = (
                    f"🎉 Congratulations {winner_mentions}! "
                    f"You won **{giveaway['prize']}**."
                )
            else:
                announcement = f"No valid entries for the **{giveaway['prize']}** giveaway; no winner was selected."

            try:
                await channel.send(
                    announcement,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException:
                pass

    @giveaway_watcher.before_loop
    async def before_giveaway_watcher(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaway(bot))
