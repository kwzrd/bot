import asyncio
import logging
import random
import textwrap
import typing
from collections import Counter
from datetime import datetime, timedelta
from typing import List, Optional, Union

from dateutil.parser import isoparse
from dateutil.relativedelta import relativedelta
from discord import Emoji, Member, Message, TextChannel
from discord.ext.commands import Context

from bot.api import ResponseCodeError
from bot.bot import Bot
from bot.constants import Channels, Guild, Roles
from bot.utils.scheduling import Scheduler
from bot.utils.time import get_time_delta, humanize_delta, time_since

if typing.TYPE_CHECKING:
    from bot.exts.recruitment.talentpool._cog import TalentPool

log = logging.getLogger(__name__)

# Maximum amount of days before an automatic review is posted.
MAX_DAYS_IN_POOL = 30

# Maximum amount of characters allowed in a message
MAX_MESSAGE_SIZE = 2000


class Reviewer:
    """Schedules, formats, and publishes reviews of helper nominees."""

    def __init__(self, name: str, bot: Bot, pool: 'TalentPool'):
        self.bot = bot
        self._pool = pool
        self._review_scheduler = Scheduler(name)

    def __contains__(self, user_id: int) -> bool:
        """Return True if the user with ID user_id is scheduled for review, False otherwise."""
        return user_id in self._review_scheduler

    async def reschedule_reviews(self) -> None:
        """Reschedule all active nominations to be reviewed at the appropriate time."""
        log.trace("Rescheduling reviews")
        await self.bot.wait_until_guild_available()
        # TODO Once the watch channel is removed, this can be done in a smarter way, e.g create a sync function.
        await self._pool.fetch_user_cache()

        for user_id, user_data in self._pool.watched_users.items():
            if not user_data["reviewed"]:
                self.schedule_review(user_id)

    def schedule_review(self, user_id: int) -> None:
        """Schedules a single user for review."""
        log.trace(f"Scheduling review of user with ID {user_id}")

        user_data = self._pool.watched_users[user_id]
        inserted_at = isoparse(user_data['inserted_at']).replace(tzinfo=None)
        review_at = inserted_at + timedelta(days=MAX_DAYS_IN_POOL)

        # If it's over a day overdue, it's probably an old nomination and shouldn't be automatically reviewed.
        if datetime.utcnow() - review_at < timedelta(days=1):
            self._review_scheduler.schedule_at(review_at, user_id, self.post_review(user_id, update_database=True))

    async def post_review(self, user_id: int, update_database: bool) -> None:
        """Format the review of a user and post it to the nomination voting channel."""
        review, seen_emoji = await self.make_review(user_id)
        if not review:
            return

        guild = self.bot.get_guild(Guild.id)
        channel = guild.get_channel(Channels.nomination_voting)

        log.trace(f"Posting the review of {user_id}")
        message = (await self._bulk_send(channel, review))[-1]
        if seen_emoji:
            for reaction in (seen_emoji, "\U0001f44d", "\U0001f44e"):
                await message.add_reaction(reaction)

        if update_database:
            nomination = self._pool.watched_users[user_id]
            await self.bot.api_client.patch(f"{self._pool.api_endpoint}/{nomination['id']}", json={"reviewed": True})

    async def make_review(self, user_id: int) -> typing.Tuple[str, Optional[Emoji]]:
        """Format a generic review of a user and return it with the seen emoji."""
        log.trace(f"Formatting the review of {user_id}")

        nomination = self._pool.watched_users[user_id]
        if not nomination:
            log.trace(f"There doesn't appear to be an active nomination for {user_id}")
            return "", None

        guild = self.bot.get_guild(Guild.id)
        member = guild.get_member(user_id)

        if not member:
            return (
                f"I tried to review the user with ID `{user_id}`, but they don't appear to be on the server :pensive:"
            ), None

        opening = f"<@&{Roles.moderators}> <@&{Roles.admins}>\n{member.mention} ({member}) for Helper!"

        current_nominations = "\n\n".join(
            f"**<@{entry['actor']}>:** {entry['reason'] or '*no reason given*'}" for entry in nomination['entries']
        )
        current_nominations = f"**Nominated by:**\n{current_nominations}"

        review_body = await self._construct_review_body(member)

        seen_emoji = self._random_ducky(guild)
        vote_request = (
            "*Refer to their nomination and infraction histories for further details*.\n"
            f"*Please react {seen_emoji} if you've seen this post."
            " Then react :+1: for approval, or :-1: for disapproval*."
        )

        review = "\n\n".join((opening, current_nominations, review_body, vote_request))
        return review, seen_emoji

    async def _construct_review_body(self, member: Member) -> str:
        """Formats the body of the nomination, with details of activity, infractions, and previous nominations."""
        activity = await self._activity_review(member)
        infractions = await self._infractions_review(member)
        prev_nominations = await self._previous_nominations_review(member)

        body = f"{activity}\n\n{infractions}"
        if prev_nominations:
            body += f"\n\n{prev_nominations}"
        return body

    async def _activity_review(self, member: Member) -> str:
        """
        Format the activity of the nominee.

        Adds details on how long they've been on the server, their total message count,
        and the channels they're the most active in.
        """
        log.trace(f"Fetching the metricity data for {member.id}'s review")
        try:
            user_activity = await self.bot.api_client.get(f"bot/users/{member.id}/metricity_review_data")
        except ResponseCodeError as e:
            if e.status == 404:
                log.trace(f"The user {member.id} seems to have no activity logged in Metricity.")
                messages = "no"
                channels = ""
            else:
                log.trace(f"An unexpected error occured while fetching information of user {member.id}.")
                raise
        else:
            log.trace(f"Activity found for {member.id}, formatting review.")
            messages = user_activity["total_messages"]
            # Making this part flexible to the amount of expected and returned channels.
            first_channel = user_activity["top_channel_activity"][0]
            channels = f", with {first_channel[1]} messages in {first_channel[0]}"

            if len(user_activity["top_channel_activity"]) > 1:
                channels += ", " + ", ".join(
                    f"{count} in {channel}" for channel, count in user_activity["top_channel_activity"][1: -1]
                )
                last_channel = user_activity["top_channel_activity"][-1]
                channels += f", and {last_channel[1]} in {last_channel[0]}"

        time_on_server = humanize_delta(relativedelta(datetime.utcnow(), member.joined_at), max_units=2)
        review = (
            f"{member.name} has been on the server for **{time_on_server}**"
            f" and has **{messages} messages**{channels}."
        )

        return review

    async def _infractions_review(self, member: Member) -> str:
        """
        Formats the review of the nominee's infractions, if any.

        The infractions are listed by type and amount, and it is stated how long ago the last one was issued.
        """
        log.trace(f"Fetching the infraction data for {member.id}'s review")
        infraction_list = await self.bot.api_client.get(
            'bot/infractions/expanded',
            params={'user__id': str(member.id), 'ordering': '-inserted_at'}
        )

        log.trace(f"{len(infraction_list)} infractions found for {member.id}, formatting review.")
        if not infraction_list:
            return "They have no infractions."

        # Count the amount of each type of infraction.
        infr_stats = list(Counter(infr["type"] for infr in infraction_list).items())

        # Format into a sentence.
        if len(infr_stats) == 1:
            infr_type, count = infr_stats[0]
            infractions = f"{count} {self._format_infr_name(infr_type, count)}"
        else:  # We already made sure they have infractions.
            infractions = ", ".join(
                f"{count} {self._format_infr_name(infr_type, count)}"
                for infr_type, count in infr_stats[:-1]
            )
            last_infr, last_count = infr_stats[-1]
            infractions += f", and {last_count} {self._format_infr_name(last_infr, last_count)}"

        infractions = f"**{infractions}**"

        # Show when the last one was issued.
        if len(infraction_list) == 1:
            infractions += ", issued "
        else:
            infractions += ", with the last infraction issued "

        # Infractions were ordered by time since insertion descending.
        infractions += get_time_delta(infraction_list[0]['inserted_at'])

        return f"They have {infractions}."

    @staticmethod
    def _format_infr_name(infr_type: str, count: int) -> str:
        """
        Format the infraction type in a way readable in a sentence.

        Underscores are replaced with spaces, as well as *attempting* to show the appropriate plural form if necessary.
        This function by no means covers all rules of grammar.
        """
        formatted = infr_type.replace("_", " ")
        if count > 1:
            if infr_type.endswith(('ch', 'sh')):
                formatted += "e"
            formatted += "s"

        return formatted

    async def _previous_nominations_review(self, member: Member) -> Optional[str]:
        """
        Formats the review of the nominee's previous nominations.

        The number of previous nominations and unnominations are shown, as well as the reason the last one ended.
        """
        log.trace(f"Fetching the nomination history data for {member.id}'s review")
        history = await self.bot.api_client.get(
            self._pool.api_endpoint,
            params={
                "user__id": str(member.id),
                "active": "false",
                "ordering": "-inserted_at"
            }
        )

        log.trace(f"{len(history)} previous nominations found for {member.id}, formatting review.")
        if not history:
            return

        num_entries = sum(len(nomination["entries"]) for nomination in history)

        nomination_times = f"{num_entries} times" if num_entries > 1 else "once"
        rejection_times = f"{len(history)} times" if len(history) > 1 else "once"
        end_time = time_since(isoparse(history[0]['ended_at']).replace(tzinfo=None), max_units=2)

        review = (
            f"They were nominated **{nomination_times}** before"
            f", but their nomination was called off **{rejection_times}**."
            f"\nThe last one ended {end_time} with the reason: {history[0]['end_reason']}"
        )

        return review

    @staticmethod
    def _random_ducky(guild: Guild) -> Union[Emoji, str]:
        """Picks a random ducky emoji to be used to mark the vote as seen. If no duckies found returns :eyes:."""
        duckies = [emoji for emoji in guild.emojis if emoji.name.startswith("ducky")]
        if not duckies:
            return ":eyes:"
        return random.choice(duckies)

    @staticmethod
    async def _bulk_send(channel: TextChannel, text: str) -> List[Message]:
        """
        Split a text into several if necessary, and post them to the channel.

        Returns the resulting message objects.
        """
        messages = textwrap.wrap(text, width=MAX_MESSAGE_SIZE, replace_whitespace=False)
        log.trace(f"The provided string will be sent to the channel {channel.id} as {len(messages)} messages.")

        results = []
        for message in messages:
            await asyncio.sleep(1)
            results.append(await channel.send(message))

        return results

    async def mark_reviewed(self, ctx: Context, user_id: int) -> bool:
        """
        Mark an active nomination as reviewed, updating the database and canceling the review task.

        Returns True if the user was successfully marked as reviewed, False otherwise.
        """
        log.trace(f"Updating user {user_id} as reviewed")
        await self._pool.fetch_user_cache()
        if user_id not in self._pool.watched_users:
            log.trace(f"Can't find a nominated user with id {user_id}")
            await ctx.send(f"❌ Can't find a currently nominated user with id `{user_id}`")
            return False

        nomination = self._pool.watched_users[user_id]
        if nomination["reviewed"]:
            await ctx.send("❌ This nomination was already reviewed, but here's a cookie :cookie:")
            return False

        await self.bot.api_client.patch(f"{self._pool.api_endpoint}/{nomination['id']}", json={"reviewed": True})
        if user_id in self._review_scheduler:
            self._review_scheduler.cancel(user_id)

        return True

    def cancel(self, user_id: int) -> None:
        """
        Cancels the review of the nominee with ID `user_id`.

        It's important to note that this applies only until reschedule_reviews is called again.
        To permanently cancel someone's review, either remove them from the pool, or use mark_reviewed.
        """
        log.trace(f"Canceling the review of user {user_id}.")
        self._review_scheduler.cancel(user_id)

    def cancel_all(self) -> None:
        """
        Cancels all reviews.

        It's important to note that this applies only until reschedule_reviews is called again.
        To permanently cancel someone's review, either remove them from the pool, or use mark_reviewed.
        """
        log.trace("Canceling all reviews.")
        self._review_scheduler.cancel_all()
