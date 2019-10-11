from datetime import datetime, timedelta
import queue
import discord
from discord.ext import tasks,commands
from services.contest import ContestTracking, Prompts, Contest
import services.config
from ext import custom_permissions

class DrawingTracking(ContestTracking):
    """
    Drawing Contest Tracking/Configuration

    """
    def __init__(self, guild_id: str, channel_id: str=None, **kwargs):
        self.guild_id = guild_id
        self.channel_id = channel_id
        super(DrawingTracking, self).__init__(**kwargs)

    @classmethod
    def deserialize(cls, data):
        guild_id = data.get('guild_id')
        channel_id = data.get('channel_id')
        prompt_index = data.get('prompt_index')
        prompts = Prompts.deserialize(data.get('prompts'))
        previous_contest = Contest.deserialize(data.get('previous_contest'))
        current_contest = Contest.deserialize(data.get('current_contest'))
        return cls(guild_id=guild_id, channel_id=channel_id,
                prompt_index=prompt_index, prompts=prompts,
                previous_contest=previous_contest, current_contest=current_contest)

    def serialize(self) -> str:
        return self.__dict__

    def set_channel_id(self, channel_id: str) -> None:
        self.channel_id = channel_id

class DrawingContest(commands.Cog):
    """
    Drawing Contest Commands

    - Runs a drawing contest and collects entries
    - Uses a per-guild prompt list
    - Allows one contest series per guild (expects to monitor one channel)
    """

    def __init__(self, bot):
        self.bot = bot
        self.execution_queue = []
        self.contests = dict()
        config = services.config.get('drawing_contest')
        if config:
            for guild_id, drawing_contest in config.items():
                dc = DrawingTracking.deserialize(drawing_contest)
                self.contests[guild_id] = dc
                current_contest = dc.current_contest
                if current_contest is not None:
                    contest_end = dc.get_contest_end()
                    self.execution_queue.append((contest_end, guild_id))
            self.execution_queue.sort(key=lambda tup: tup[0])

    def save_contests(self):
        services.config.set('drawing_contest', self.contests)
        services.config.save()

    @commands.Cog.listener()
    async def on_ready(self):
        """Load the timers for each contest series"""
        self.prompt_timer.start()

    @commands.Cog.listener()
    async def on_message(self, message):
        if self.message_is_drawing_maybe(message):
            contest = self.get_contest(message.guild)
            contest.add_entry(str(message.id))
            await message.add_reaction('👍')
            self.save_contests()
        else:
            print("What is this")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, reaction):
        if reaction.user_id == self.bot.user.id:
            return
        channel = self.bot.get_channel(int(reaction.channel_id))
        message = await channel.fetch_message(reaction.message_id)
        if self.message_is_drawing_maybe(message):
            contest = self.get_contest(message.guild)
            entry = contest.get_entry(str(message.id))
            if entry is None:
                return
            print("got entry")
            emoji = reaction.emoji
            if emoji is None:
                return
            print("got emoji")
            if str(emoji) == '👎':
                print("removing")
                entry["removed"] = True
                await message.remove_reaction('👍', self.bot.user)
                self.save_contests()

    def message_is_drawing_maybe(self, message: discord.Message):
        # we're looking for user messages with attachments in our guild
        # drawing channel...
        if message.type != discord.MessageType.default:
            return False
        if len(message.attachments) < 1:
            return False
        if message.guild is None:
            return False
        if str(message.guild.id) in self.contests:
            channel_id = self.contests[str(message.guild.id)].channel_id
            if str(message.channel.id) == channel_id:
                print("Guild channel match")
                return True

    def cog_unload(self):
        self.prompt_timer.cancel()

    @tasks.loop(seconds=60)
    async def prompt_timer(self):
        now = datetime.now()
        for contest_end, guild_id in self.execution_queue:
            if now > contest_end:
                await self.end_contest(guild_id)
                await self.start_contest(guild_id)
            else:
                break

    def get_contest(self, guild: str):
        guild_id = str(guild.id)
        if guild_id in self.contests:
            contest = self.contests[guild_id]
        else:
            contest = DrawingTracking(guild_id=guild_id)
            self.contests[guild_id] = contest
        return contest

    async def end_contest(self, guild: str):
        contest = self.get_contest(guild)
        channel = self.bot.get_channel(int(contest.channel_id))
        async with channel.typing():
            if contest.stop_contest():
                self.save_contests()
                await channel.send('Drawing Contest has ended :(')
            else:
                await channel.send('I couldn\'t find a contest to end')

    async def start_contest(self, guild: str, interval_days: int=None):
        contest = self.get_contest(guild)
        channel = self.bot.get_channel(int(contest.channel_id))
        async with channel.typing():
            contest.start_contest(interval_days)
            prompt = contest.get_current_prompt()
            await self.say_prompt(channel, prompt)
            contest_end = contest.get_contest_end()
            if contest_end is not None:
                self.save_contests()
                self.execution_queue.append((contest_end, guild.id))
                await channel.send('Contest ends at {}'.format(contest_end))

    async def say_prompt(self, channel: discord.abc.Messageable, prompt: str=None):
        if prompt is None:
            await channel.send('I\'m all out of drawing prompts!')
        else:
            await channel.send('Draw: **{}**!'.format(prompt))

    @commands.group(name="draw")
    async def draw(self, ctx):
        """Drawing Contest Commands"""

    @draw.group(name="contest", description="Drawing Contest functions")
    async def contest(self, ctx):
        """Drawing Contest Functions"""

    @contest.command(description="Start the contest!")
    @commands.guild_only()
    @commands.has_permissions(manage_webhooks=True)
    async def start(self, ctx, interval_days: int):
        """Sets the drawing contest timer for the specified amount of days"""
        contest = self.get_contest(ctx.guild)
        channel_id = str(ctx.channel.id)
        print("setting contest channel" + channel_id)
        contest.set_channel_id(channel_id)
        print("channel id:" + contest.channel_id)
        await self.start_contest(ctx.guild, interval_days)

    @contest.command(description="Stop the contest :(")
    @commands.guild_only()
    @commands.has_permissions(manage_webhooks=True)
    async def stop(self, ctx):
        """Ends the current drawing contest and unsets the next execution"""
        await self.end_contest(ctx.guild)

    @draw.group(
            name="prompt",
            invoke_without_command=True,
            description="Get a drawing prompt"
            )
    @commands.guild_only()
    async def prompt(self, ctx):
        """Get a drawing prompt"""
        async with ctx.typing():
            contest = self.get_contest(ctx.guild)
            prompt = contest.get_current_prompt()
            self.save_contests()
            await self.say_prompt(ctx, prompt)

    @prompt.command(description='Add a drawing subject')
    @commands.guild_only()
    async def add(self, ctx, *, phrase : str):
        """Adds a drawing subject to guild prompts"""
        async with ctx.typing():
            contest = self.get_contest(ctx.guild)
            if contest.add_prompt(phrase):
                self.save_contests()
                await ctx.send('added "{}" to drawing prompts!'.format(phrase))
            else:
                await ctx.send('sorry, I didn\'t understand that one.')

    @prompt.command(description='Remove a drawing subject')
    @commands.guild_only()
    @commands.has_permissions(manage_webhooks=True)
    async def remove(self, ctx, *, phrase : str):
        """Removes a drawing subect from guild prompts"""
        async with ctx.typing():
            contest = self.get_contest(ctx.guild)
            if contest.remove_prompt(phrase):
                self.save_contests()
                await ctx.send('removed "{}" from drawing prompts!'.format(phrase))
            else:
                await ctx.send('sorry, I didn\'t understand that one.')

    @prompt.command(description='Shuffle drawing subjects',hidden=True)
    @commands.guild_only()
    @commands.has_permissions(manage_webhooks=True)
    async def shuffle(self, ctx):
        """Shuffles guild prompts"""
        async with ctx.typing():
            contest = self.get_contest(ctx.guild)
            contest.shuffle_prompts()
            self.save_contests()
            await channel.send('Done!')

