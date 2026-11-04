"""Real Discord delivery drill, separate from production trainee assignments."""
import asyncio
import uuid
import discord

DRILLS = {}
START_LOCK = asyncio.Lock()


def status(actor_id):
    drill = DRILLS.get(actor_id)
    if not drill:
        return {"test": "trainnex-drill", "status": "No interactive drill in this bot session.", "events": []}
    return {"test": "trainnex-drill", "status": drill.state, "drillId": drill.id,
            "events": drill.events[-20:], "active": drill.active,
            "mode": "Real Discord delivery and button drill; no production assignment."}


class DrillView(discord.ui.View):
    def __init__(self, drill, assigned=None):
        super().__init__(timeout=120 if assigned else 300)
        self.drill, self.assigned = drill, assigned
        for child in self.children:
            child.custom_id = f"trainnex:drill:{drill.id}:{uuid.uuid4().hex[:8]}"

    async def respond(self, interaction, outcome):
        async with self.drill.lock:
            if not self.drill.active or self is not self.drill.view:
                return await interaction.response.send_message("This test step has ended.", ephemeral=True)
            if outcome == 'cancel':
                allowed = interaction.user.id == self.drill.actor.id
            else:
                allowed = interaction.user.id == self.assigned if self.assigned else interaction.user.id in self.drill.trainers
            if not allowed:
                return await interaction.response.send_message("Only the selected test participant can use this button (or the organizer to cancel).", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            if outcome == 'claim':
                self.drill.events.append(f"Claim clicked by {interaction.user.id}.")
                await self.drill.finish('Claim completed', claimant=interaction.user)
            elif outcome == 'cancel':
                await self.drill.finish('Cancelled by organizer')
            else:
                self.drill.events.append(f"{'Expired early' if outcome == 'expire' else 'Passed'} by {interaction.user.id}.")
                await self.drill.advance()
            await interaction.followup.send(self.drill.state, ephemeral=True)

    @discord.ui.button(label="TEST Claim", style=discord.ButtonStyle.success, emoji="✅")
    async def claim(self, interaction, button):
        await self.respond(interaction, 'claim')

    @discord.ui.button(label="TEST Pass", style=discord.ButtonStyle.secondary, emoji="➡️")
    async def pass_offer(self, interaction, button):
        if self.assigned is None:
            return await interaction.response.send_message("Fallback is already open. Claim it or let the drill expire.", ephemeral=True)
        await self.respond(interaction, 'pass')

    @discord.ui.button(label="Expire now", style=discord.ButtonStyle.secondary, emoji="⏱️")
    async def expire(self, interaction, button):
        if self.assigned is None:
            return await interaction.response.send_message("Fallback is already open.", ephemeral=True)
        await self.respond(interaction, 'expire')

    @discord.ui.button(label="Cancel drill", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        await self.respond(interaction, 'cancel')

    async def on_timeout(self):
        async with self.drill.lock:
            if self.drill.active and self is self.drill.view:
                self.drill.events.append("Offer timed out." if self.assigned else "Fallback timed out.")
                if self.assigned:
                    await self.drill.advance()
                else:
                    await self.drill.finish('Expired without a claim')


class Drill:
    def __init__(self, runtime, actor, trainers):
        self.runtime, self.actor = runtime, actor
        self.trainers = {member.id: member for member in trainers}
        self.remaining = list(trainers)
        self.id = uuid.uuid4().hex[:12]
        self.lock = asyncio.Lock()
        self.state, self.active = 'Starting', True
        self.events = ['Interactive drill started; real trainee queue is unchanged.']
        self.view, self.message = None, None

    def embed(self, description):
        embed = discord.Embed(title=f"🧪 TrainNex TEST • {self.id}", description=description,
                              color=discord.Color.gold())
        embed.set_footer(text="TEST ONLY • Do not begin real training • Expires automatically")
        return embed

    async def retire(self):
        if self.view:
            self.view.stop()
        if self.message:
            try:
                await self.message.edit(embed=self.embed(self.state), view=None)
            except discord.HTTPException:
                self.events.append('Could not remove buttons from the previous test message.')

    async def advance(self):
        await self.retire()
        self.view, self.message = None, None
        while self.remaining:
            member = self.remaining.pop(0)
            self.view = DrillView(self, member.id)
            self.state = f'Waiting for test offer response from {member.display_name}'
            try:
                self.message = await member.send(embed=self.embed(
                    f"Test organized by {self.actor.mention}. You are the selected test FTO.\n"
                    "Claim to test the notification, Pass to route to the next selected FTO, or Expire now to exercise timeout routing.\n"
                    "This test offer expires in two minutes. No real trainee is assigned."),
                    view=self.view, allowed_mentions=discord.AllowedMentions.none())
                self.events.append(f'Test offer DM delivered to {member.id}.')
                return
            except discord.HTTPException:
                self.view.stop()
                self.events.append(f'Test offer DM failed for {member.id}; trying the next participant.')
        try:
            channel = await self.runtime.get_training_channel(self.runtime.ANNOUNCEMENT_CHANNEL_ID)
            if channel is None:
                raise ValueError('Fallback channel unavailable')
            self.view = DrillView(self)
            self.state = 'Waiting for a selected participant to claim the test fallback'
            self.message = await channel.send(embed=self.embed(
                "All selected test offers were passed, expired, or undeliverable.\n"
                "Only the selected test participants can claim this drill.\n"
                "This fallback expires in five minutes. No trainee assignment or role ping is created."),
                view=self.view, allowed_mentions=discord.AllowedMentions.none())
            self.events.append('Test fallback delivered to the configured announcement channel.')
        except (discord.HTTPException, ValueError):
            await self.finish('Failed: fallback message could not be delivered')

    async def finish(self, state, claimant=None):
        self.active, self.state = False, state
        self.events.append(state)
        await self.retire()
        if claimant:
            try:
                channel = await self.runtime.get_training_channel(self.runtime.CLAIM_NOTIFICATION_CHANNEL_ID)
                if channel is None:
                    raise ValueError('Claim channel unavailable')
                await channel.send(embed=self.embed(f"Test claim completed by {claimant.mention}. No real trainee was assigned."),
                                   allowed_mentions=discord.AllowedMentions.none())
                self.events.append('Test claim notification delivered to the configured claim channel.')
            except (discord.HTTPException, ValueError):
                self.state = 'Claim clicked; claim notification delivery FAILED'
                self.events.append(self.state)
        try:
            await self.actor.send(embed=self.embed(self.state + '\n\n' + '\n'.join(self.events[-12:])),
                                  allowed_mentions=discord.AllowedMentions.none())
            self.events.append('Organizer result DM delivered.')
        except discord.HTTPException:
            self.events.append('Organizer result DM failed; use portal Refresh drill result.')


async def start(runtime, actor, trainer_ids):
    async with START_LOCK:
        return await _start(runtime, actor, trainer_ids)


async def _start(runtime, actor, trainer_ids):
    existing = DRILLS.get(actor.id)
    if existing and existing.active:
        raise ValueError('Your interactive drill is still active. Cancel it before starting another.')
    guild = runtime.bot.get_guild(runtime.TRAINING_SERVER_ID)
    trainers = []
    for member_id in dict.fromkeys(trainer_ids):
        member = guild.get_member(member_id) or await guild.fetch_member(member_id)
        if member.bot or not runtime.has_fto_role(member):
            raise ValueError('Select test participants with the configured FTO role in the training server.')
        trainers.append(member)
    if not trainers or len(trainers) > 2:
        raise ValueError('Select one or two test FTOs.')
    for drill in DRILLS.values():
        if drill.active and set(drill.trainers).intersection(member.id for member in trainers):
            raise ValueError('A selected participant is already in an active drill.')
    # Retain a bounded number of completed session reports.
    for key in list(DRILLS):
        if len(DRILLS) < 50:
            break
        if not DRILLS[key].active:
            del DRILLS[key]
    if len(DRILLS) >= 50:
        raise ValueError('Too many concurrent drills. Wait for an existing drill to finish.')
    drill = Drill(runtime, actor, trainers)
    DRILLS[actor.id] = drill
    async with drill.lock:
        await drill.advance()
    return status(actor.id)


async def cancel(actor_id):
    drill = DRILLS.get(actor_id)
    if drill:
        async with drill.lock:
            if drill.active:
                await drill.finish('Cancelled by organizer')
    return status(actor_id)
