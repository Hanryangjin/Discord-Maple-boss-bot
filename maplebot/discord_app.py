from __future__ import annotations
import asyncio
import logging
import os
import time
import discord
from discord import app_commands
from discord.ext import commands
from .db import Database
from .service import Actor, Service
from .validation import UserError, key
from .render import boss_pages, character_pages, pages, safe
from .board import BoardWorker

log = logging.getLogger(__name__)
STATES = [app_commands.Choice(name='활성', value='active'), app_commands.Choice(name='비활성', value='inactive'), app_commands.Choice(name='전체', value='all')]
BOSS_STATES = STATES + [app_commands.Choice(name='삭제', value='deleted')]


def actor(interaction):
    if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
        raise UserError('서버에서만 사용할 수 있습니다.')
    return Actor(interaction.guild_id, interaction.user.id, interaction.user.guild_permissions.administrator,
                 tuple(r.id for r in interaction.user.roles))


async def send_pages(interaction, content):
    view = Pager(interaction.user.id, content)
    embed = view.embed()
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class PrivateView(discord.ui.View):
    def __init__(self, user_id, timeout=300):
        super().__init__(timeout=timeout)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('이 화면을 연 사용자만 사용할 수 있습니다.', ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        await report(interaction, error)


class Pager(PrivateView):
    def __init__(self, user_id, content):
        super().__init__(user_id)
        self.content = content
        self.index = 0

    def embed(self):
        result = discord.Embed(**self.content[self.index], color=0x65AD86)
        result.set_footer(text=f'{self.index+1}/{len(self.content)}')
        return result

    @discord.ui.button(label='이전', style=discord.ButtonStyle.secondary)
    async def previous(self, i, button):
        self.index = (self.index-1) % len(self.content)
        await i.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label='다음', style=discord.ButtonStyle.secondary)
    async def next(self, i, button):
        self.index = (self.index+1) % len(self.content)
        await i.response.edit_message(embed=self.embed(), view=self)


async def report(i, error):
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    message = str(error) if isinstance(error, UserError) else '작업을 처리하지 못했습니다. 잠시 후 재시도하거나 관리자에게 문의해 주세요.'
    if not isinstance(error, UserError):
        log.error('interaction failed: %s', type(error).__name__, exc_info=error)
    if i.response.is_done():
        await i.followup.send(message[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await i.response.send_message(message[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class Confirm(PrivateView):
    def __init__(self, app, user, action, payload, token):
        super().__init__(user)
        self.app, self.action, self.payload, self.token = app, action, payload, token

    @discord.ui.button(label='확인', style=discord.ButtonStyle.danger)
    async def confirm(self, i, button):
        await self.app.mutate(i, self.action, self.payload, self.token)
        for child in self.children:
            child.disabled = True
        try:
            await i.message.edit(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label='취소', style=discord.ButtonStyle.secondary)
    async def cancel(self, i, button):
        self.stop()
        await i.response.edit_message(content='취소했습니다. 변경하지 않았습니다.', view=None, embed=None)


class DiscordTransport:
    def __init__(self, bot):
        self.bot = bot

    async def channel(self, channel_id):
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                raise LookupError('unsupported channel')
            return channel
        except discord.NotFound as e:
            raise LookupError('channel missing') from e
        except discord.Forbidden as e:
            raise PermissionError('channel forbidden') from e

    def embed(self, body, marker):
        embed = discord.Embed(**body, color=0x65AD86)
        embed.set_footer(text=marker)
        return embed

    async def send(self, channel_id, body, marker):
        try:
            channel = await self.channel(channel_id)
            message = await channel.send(embed=self.embed(body, marker), allowed_mentions=discord.AllowedMentions.none(), view=BoardView(self.bot))
            return message.id
        except discord.Forbidden as e:
            raise PermissionError('send forbidden') from e

    async def edit(self, channel_id, mid, body, marker):
        try:
            channel = await self.channel(channel_id)
            message = await channel.fetch_message(mid)
            if message.author.id != self.bot.user.id:
                raise PermissionError('message owner mismatch')
            await message.edit(embed=self.embed(body, marker), content=None,
                               allowed_mentions=discord.AllowedMentions.none(), view=None if marker=='maple:retired' else BoardView(self.bot))
            return True
        except discord.NotFound:
            return False
        except discord.Forbidden as e:
            raise PermissionError('edit forbidden') from e

    async def delete(self, channel_id, mid):
        try:
            channel = await self.channel(channel_id)
            message = await channel.fetch_message(mid)
            if message.author.id != self.bot.user.id:
                raise PermissionError('message owner mismatch')
            await message.delete()
        except discord.NotFound:
            return
        except discord.Forbidden as e:
            raise PermissionError('delete forbidden') from e
        except LookupError:
            return  # Deleted channel implies deleted messages.

    async def scan(self, channel_id, guild_id):
        channel = await self.channel(channel_id)
        found = []
        try:
            async for message in channel.history(limit=100):
                if message.author.id != self.bot.user.id or not message.embeds:
                    continue
                marker = message.embeds[0].footer.text or ''
                parts = marker.split(':')
                if len(parts)==5 and parts[0]=='maple' and all(x.isdecimal() for x in parts[1:]) and int(parts[1])==guild_id:
                    found.append((int(parts[2]),int(parts[3]),int(parts[4]),message.id))
        except discord.Forbidden as e:
            raise PermissionError('history forbidden') from e
        return found

    async def find(self, channel_id, marker):
        channel = await self.channel(channel_id)
        try:
            found = []
            async for message in channel.history(limit=100):
                if message.author.id == self.bot.user.id and message.embeds and message.embeds[0].footer.text == marker:
                    found.append(message.id)
            if found:
                for duplicate in found[1:]:
                    await self.delete(channel_id, duplicate)
                return found[0]
        except discord.Forbidden as e:
            raise PermissionError('history forbidden') from e
        return None


class BoardView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='관리 패널', custom_id='maple:manage:v1', style=discord.ButtonStyle.secondary)
    async def panel(self, i, button):
        try:
            await self.bot.open_panel(i)
        except Exception as e:
            await report(i, e)

    @discord.ui.button(label='내 보스', custom_id='maple:mine:v1', style=discord.ButtonStyle.primary)
    async def mine(self, i, button):
        try:
            await self.bot.mine(i)
        except Exception as e:
            await report(i, e)


class MapleBot(commands.Bot):
    def __init__(self, db):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         allowed_mentions=discord.AllowedMentions.none(), proxy=os.getenv('DISCORD_PROXY') or None)
        self.service = Service(db)
        self.worker = BoardWorker(self.service, DiscordTransport(self))
        self.tree.allowed_contexts = app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False)
        self.tree.allowed_installs = app_commands.AppInstallationType(guild=True, user=False)
        self.background = []
        self.last_backup = 0

    async def setup_hook(self):
        self.add_view(BoardView(self))
        await self.add_cog(MapleCommands(self))
        target = os.getenv('DISCORD_TEST_GUILD_ID')
        if target:
            guild = discord.Object(id=int(target))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.tree.on_error = report
        self.background = [asyncio.create_task(self.run_worker()), asyncio.create_task(self.maintenance())]

    async def close(self):
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        await super().close()

    async def run_worker(self):
        await self.wait_until_ready()
        await self.worker.run()

    async def maintenance(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for guild in self.guilds:
                    for uid in await asyncio.to_thread(self.service.member_ids, guild.id):
                        await self.observe(guild, uid)
                if time.time()-self.last_backup > 86400:
                    await asyncio.to_thread(self.service.db.backup, os.getenv('MAPLEBOT_BACKUPS', 'var/backups'))
                    self.last_backup = time.time()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('maintenance failed')
            await asyncio.sleep(300)

    async def on_ready(self):
        # Reconcile missed gateway events and manually removed board messages after reconnect.
        def dirty():
            with self.service.db.transaction() as c:
                for guild in self.guilds:
                    self.service.db.initialize(c, guild.id)
                    self.service.touch(c, guild.id)
        await asyncio.to_thread(dirty)

    async def on_raw_message_delete(self, payload):
        if payload.guild_id is None:
            return
        def dirty():
            with self.service.db.transaction() as c:
                row = c.execute("""SELECT m.guild_id,m.generation,m.boss_id FROM board_messages m
                    JOIN board_generations g ON g.guild_id=m.guild_id AND g.generation=m.generation
                    WHERE m.channel_id=? AND m.message_id=? AND g.status IN('active','preparing')""",
                    (payload.channel_id,payload.message_id)).fetchone()
                if row:
                    c.execute('UPDATE board_sync_state SET requested=requested+1,retry_at=0 WHERE guild_id=? AND generation=? AND boss_id=?',tuple(row))
        await asyncio.to_thread(dirty)

    async def on_raw_bulk_message_delete(self, payload):
        for mid in payload.message_ids:
            class Deleted:
                guild_id=payload.guild_id
                channel_id=payload.channel_id
                message_id=mid
            await self.on_raw_message_delete(Deleted())

    async def observe(self, guild, uid):
        old_name = str(uid)
        with self.service.db.transaction(False) as c:
            old = c.execute('SELECT display_name FROM discord_users WHERE guild_id=? AND user_id=?', (guild.id, uid)).fetchone()
            if old:
                old_name = old[0]
        try:
            # Fetch for authoritative membership; cache absence is never a departure.
            member = await guild.fetch_member(uid)
            name, state = member.display_name, 'present'
        except discord.NotFound:
            name, state = old_name, 'left'
        except (discord.HTTPException, OSError):
            name, state = old_name, 'unknown'
        await asyncio.to_thread(self.service.observe_member, guild.id, uid, name, state)

    async def on_member_update(self, before, after):
        await asyncio.to_thread(self.service.observe_member, after.guild.id, after.id, after.display_name, 'present')

    async def on_member_join(self, member):
        await asyncio.to_thread(self.service.observe_member, member.guild.id, member.id, member.display_name, 'present')

    async def on_member_remove(self, member):
        await asyncio.to_thread(self.service.observe_member, member.guild.id, member.id, member.display_name, 'left')

    async def mutate(self, i, action, payload, token=None):
        a = actor(i)
        if not i.response.is_done():
            await i.response.defer(ephemeral=True, thinking=True)
        # Permission first: unauthorized calls cannot trigger membership probes.
        def check():
            with self.service.db.transaction() as c:
                self.service.db.initialize(c, a.guild_id)
                self.service.authorize(c, a, action, payload)
        await asyncio.to_thread(check)
        ids = set()
        if action == 'character.add':
            ids.add(payload.get('owner_id', a.user_id))
        elif action == 'character.owner':
            ids.add(payload['owner_id'])
        elif action == 'character.activate':
            ch = await asyncio.to_thread(self.entity, a, 'characters', payload['id'])
            ids.add(ch['owner_id'])
        elif action in ('group.member', 'group.move'):
            ch = await asyncio.to_thread(self.entity, a, 'characters', payload['character_id'])
            ids.add(ch['owner_id'])
        elif action == 'group.merge':
            with self.service.db.transaction(False) as c:
                ids.update(r[0] for r in c.execute('SELECT ch.owner_id FROM group_members m JOIN characters ch ON ch.id=m.character_id WHERE m.guild_id=? AND m.group_id=?', (a.guild_id, payload['id'])))
        for uid in ids:
            await self.observe(i.guild, uid)
        if action == 'board.publish':
            settings = await asyncio.to_thread(self.service.settings, a)
            cid = payload.get('channel_id') or settings['last_channel_id'] or settings['default_channel_id']
            if cid:
                channel = i.guild.get_channel(cid)
                if not isinstance(channel, discord.TextChannel):
                    raise UserError('현재 서버의 일반 텍스트 채널을 선택해 주세요.')
                perm = channel.permissions_for(i.guild.me)
                if not (perm.view_channel and perm.send_messages and perm.embed_links and perm.read_message_history):
                    raise UserError('게시 채널의 보기·전송·Embed·메시지 기록 읽기 권한이 필요합니다.')
        result = await asyncio.to_thread(self.service.execute, a, action, payload, token)
        if result['status'] == 'confirm':
            await i.followup.send(result['message'][:1900], view=Confirm(self, a.user_id, action, payload, result['token']), ephemeral=True)
        else:
            await i.followup.send(result['message'][:1900], ephemeral=True)

    def entity(self, a, table, entity_id):
        with self.service.db.transaction(False) as c:
            return self.service.get(c, a.guild_id, table, int(entity_id))

    async def boss_query(self, i, bid):
        a = actor(i)
        def query():
            with self.service.db.transaction(False) as c:
                boss = self.service.get(c, a.guild_id, 'bosses', int(bid))
                if boss['status'] != 'active' and not self.service.manager(c, a):
                    raise UserError('활성 보스만 조회할 수 있습니다.')
                return boss_pages(c, a.guild_id, int(bid), public=False)
        await send_pages(i, await asyncio.to_thread(query))

    async def char_query(self, i, cid):
        a = actor(i)
        def query():
            with self.service.db.transaction(False) as c:
                return character_pages(c, a.guild_id, int(cid))
        await send_pages(i, await asyncio.to_thread(query))

    async def mine(self, i):
        a = actor(i)
        await i.response.defer(ephemeral=True)
        def query():
            with self.service.db.transaction(False) as c:
                ids = [r[0] for r in c.execute('SELECT id FROM characters WHERE guild_id=? AND owner_id=? ORDER BY name', (a.guild_id, a.user_id))]
                return [p for cid in ids for p in character_pages(c, a.guild_id, cid)] or pages('내 보스', ['등록된 캐릭터가 없습니다.'])
        await send_pages(i, await asyncio.to_thread(query))

    async def open_panel(self, i, boss_id=None):
        a = actor(i)
        await asyncio.to_thread(self.service.settings, a)
        from .panel import MainPanel, BossPanel
        if boss_id is not None:
            boss = await asyncio.to_thread(self.entity,a,'bosses',boss_id)
            if boss['status'] == 'deleted':
                raise UserError('삭제된 보스입니다. /보스 복구를 사용해 주세요.')
            await i.response.send_message(f'{safe(boss["name"])} ({safe(boss["difficulty"])})',view=BossPanel(self,a.user_id,boss_id),ephemeral=True)
        else:
            await i.response.send_message('파티 관리 · 캐릭터 관리 · 조회', view=MainPanel(self, a.user_id), ephemeral=True)


class MapleCommands(commands.Cog):
    settings = app_commands.Group(name='설정', description='서버 설정', guild_only=True)
    bosses = app_commands.Group(name='보스', description='운영 보스 관리와 조회', guild_only=True)
    groups = app_commands.Group(name='파티', description='파티 구성 관리', guild_only=True)
    chars = app_commands.Group(name='캐릭터', description='캐릭터 관리와 조회', guild_only=True)
    jobs = app_commands.Group(name='직업', description='관리되는 직업 목록', guild_only=True)
    boards = app_commands.Group(name='파티배분표', description='공개 파티 배분표', guild_only=True)
    manage = app_commands.Group(name='관리', description='관리 화면', guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    async def choices(self, i, current, table, *, state='active', own=False):
        a = actor(i)
        rows = await asyncio.to_thread(self.bot.service.rows, a.guild_id, table, actor=a, state=state)
        if own:
            with self.bot.service.db.transaction(False) as c:
                manager = self.bot.service.manager(c, a)
            if not manager:
                rows = [r for r in rows if r['owner_id'] == a.user_id]
        bosses = {}
        if table == 'boss_groups':
            bosses = {r['id']: r for r in await asyncio.to_thread(self.bot.service.rows, a.guild_id, 'bosses', actor=a, state='all')}
        result = []
        for row in rows:
            name = row['name']
            if table == 'bosses':
                name += f' ({row["difficulty"]}) · {row["status"]}'
            if table == 'boss_groups':
                b = bosses[row['boss_id']]
                name = f'{b["name"]} ({b["difficulty"]}) / {name} · #{row["id"]}'
            if key(current) in key(name):
                result.append(app_commands.Choice(name=name[:100], value=str(row['id'])))
        return result[:25]

    async def ac_boss(self, i, current):
        a = actor(i)
        with self.bot.service.db.transaction(False) as c:
            manager = self.bot.service.manager(c, a)
        return await self.choices(i, current, 'bosses', state='all' if manager else 'active')

    async def ac_group(self, i, current):
        a = actor(i)
        await asyncio.to_thread(self.bot.service.settings, a)
        return await self.choices(i, current, 'boss_groups', state='all')

    async def ac_char(self, i, current):
        return await self.choices(i, current, 'characters', state='all')

    async def ac_char_active(self, i, current):
        return await self.choices(i, current, 'characters', state='active')

    async def ac_own(self, i, current):
        return await self.choices(i, current, 'characters', state='all', own=True)

    async def ac_job(self, i, current):
        return await self.choices(i, current, 'jobs')

    async def ac_job_all(self, i, current):
        return await self.choices(i, current, 'jobs', state='all')

    @settings.command(name='관리자역할', description='파티 배분 관리자 역할 지정 (서버 관리자)')
    async def role(self, i: discord.Interaction, 역할: discord.Role):
        if 역할.is_default():
            raise UserError('@everyone을 관리자 역할로 지정할 수 없습니다.')
        await self.bot.mutate(i, 'settings.role', {'value': 역할.id})

    @settings.command(name='파티배분표채널', description='기본 게시 채널 지정 (서버 관리자)')
    async def channel(self, i: discord.Interaction, 채널: discord.TextChannel):
        await self.bot.mutate(i, 'settings.channel', {'value': 채널.id})

    @settings.command(name='조회', description='설정과 게시 작업·오류 상태 조회')
    async def config(self, i: discord.Interaction):
        data = await asyncio.to_thread(self.bot.service.settings, actor(i))
        lines = [f'관리자 역할: <@&{data["manager_role_id"]}>' if data['manager_role_id'] else '관리자 역할: 미지정',
                 f'기본 채널: <#{data["default_channel_id"]}>', f'마지막 게시 채널: <#{data["last_channel_id"]}>',
                 '게시 중' if data['active_generation'] is not None else '게시 중단 또는 미게시',
                 '새 채널 게시 준비 중' if data['preparing_generation'] is not None else '준비 작업 없음']
        lines += [f'게시 세대 {r["generation"]}: {r["status"]} / 오류: {r["last_error"] or "없음"}' for r in data['generations']]
        lines += [f'보스 #{r["boss_id"]}: {r["last_error"]}' for r in data['errors']]
        lines += [f'이전 게시물 정리 실패: <#{r["channel_id"]}> / {r["last_error"]}' for r in data['cleanup_errors']]
        await send_pages(i, pages('서버 설정', lines))

    @jobs.command(name='목록', description='직업 목록 조회')
    @app_commands.choices(상태=STATES)
    async def job_list(self, i: discord.Interaction, 상태: str='active'):
        rows = await asyncio.to_thread(self.bot.service.rows, actor(i).guild_id, 'jobs', actor=actor(i), state=상태)
        await send_pages(i, pages('직업 목록', [safe(r['name'])+(' (비활성)' if not r['active'] else '') for r in rows]))

    @jobs.command(name='추가', description='직업 추가 (서버 관리자)')
    async def job_add(self, i: discord.Interaction, 이름: str):
        await self.bot.mutate(i, 'job.add', {'name': 이름})

    @jobs.command(name='이름변경', description='직업 이름 변경 (서버 관리자)')
    @app_commands.autocomplete(직업=ac_job_all)
    async def job_rename(self, i: discord.Interaction, 직업: str, 이름: str):
        await self.bot.mutate(i, 'job.rename', {'id': entity_id(직업), 'name': 이름})

    @jobs.command(name='비활성화', description='직업 신규 선택 차단 (서버 관리자)')
    @app_commands.autocomplete(직업=ac_job)
    async def job_off(self, i: discord.Interaction, 직업: str):
        await self.bot.mutate(i, 'job.deactivate', {'id': entity_id(직업)})

    @jobs.command(name='활성화', description='직업 신규 선택 재개 (서버 관리자)')
    @app_commands.autocomplete(직업=ac_job_all)
    async def job_on(self, i: discord.Interaction, 직업: str):
        await self.bot.mutate(i, 'job.activate', {'id': entity_id(직업)})

    @bosses.command(name='추가', description='보스 등록, 파티는 자동 생성하지 않음')
    async def boss_add(self, i: discord.Interaction, 이름: str, 난이도: str, 최대인원: app_commands.Range[int,1,6]=6):
        await self.bot.mutate(i, 'boss.add', {'name': 이름, 'difficulty': 난이도, 'max_members': 최대인원})

    @bosses.command(name='목록', description='운영 보스 목록')
    @app_commands.choices(상태=BOSS_STATES)
    async def boss_list(self, i: discord.Interaction, 상태: str='active'):
        rows = await asyncio.to_thread(self.bot.service.rows, actor(i).guild_id, 'bosses', actor=actor(i), state=상태)
        await send_pages(i, pages('보스 목록', [f'{safe(r["name"])} ({safe(r["difficulty"])}) · 정원 {r["max_members"]} · {r["status"]}' for r in rows]))

    @bosses.command(name='수정', description='보스 속성과 정원 수정')
    @app_commands.autocomplete(보스=ac_boss)
    async def boss_edit(self, i: discord.Interaction, 보스: str, 이름: str|None=None, 난이도: str|None=None,
                        표시이름: str|None=None, 표시순서: int|None=None, 최대인원: app_commands.Range[int,1,6]|None=None):
        p = {'id': entity_id(보스)}
        p.update({k:v for k,v in [('name',이름),('difficulty',난이도),('display_name',표시이름),('sort_order',표시순서),('max_members',최대인원)] if v is not None})
        await self.bot.mutate(i, 'boss.edit', p)

    @bosses.command(name='비활성화', description='보스 운영 중단, 배정 보존')
    @app_commands.autocomplete(보스=ac_boss)
    async def boss_off(self, i: discord.Interaction, 보스: str):
        await self.bot.mutate(i, 'boss.deactivate', {'id':entity_id(보스)})

    @bosses.command(name='활성화', description='비활성 보스 운영 재개')
    @app_commands.autocomplete(보스=ac_boss)
    async def boss_on(self, i: discord.Interaction, 보스: str):
        await self.bot.mutate(i, 'boss.activate', {'id':entity_id(보스)})

    @bosses.command(name='삭제', description='보스·파티 소프트 삭제 (서버 관리자)')
    @app_commands.autocomplete(보스=ac_boss)
    async def boss_delete(self, i: discord.Interaction, 보스: str):
        await self.bot.mutate(i, 'boss.delete', {'id':entity_id(보스)})

    @bosses.command(name='복구', description='삭제 보스를 비활성 상태로 복구 (서버 관리자)')
    @app_commands.autocomplete(보스=ac_boss)
    async def boss_restore(self, i: discord.Interaction, 보스: str):
        await self.bot.mutate(i, 'boss.restore', {'id':entity_id(보스)})

    @bosses.command(name='현황', description='보스별 파티와 구성원 조회')
    @app_commands.autocomplete(보스=ac_boss)
    async def boss_show(self, i: discord.Interaction, 보스: str):
        await self.bot.boss_query(i, entity_id(보스))

    @chars.command(name='등록', description='캐릭터 등록, 관리자는 소유자 지정 가능')
    @app_commands.describe(헥환='만 단위 숫자. 예: 5.25 → 5.25만')
    @app_commands.autocomplete(직업=ac_job)
    async def char_add(self, i: discord.Interaction, 이름: str, 직업: str, 헥환: str|None=None, 소유자: discord.Member|None=None):
        p = {'name':이름,'job_id':entity_id(직업),'owner_id':소유자.id if 소유자 else i.user.id}
        if 헥환 is not None: p['hexa'] = 헥환
        await self.bot.mutate(i, 'character.add', p)

    @chars.command(name='수정', description='본인 또는 관리자 캐릭터 수정')
    @app_commands.describe(헥환='만 단위 숫자. 예: 5.25 → 5.25만')
    @app_commands.autocomplete(캐릭터=ac_own, 직업=ac_job)
    async def char_edit(self, i: discord.Interaction, 캐릭터: str, 이름: str|None=None, 직업: str|None=None, 헥환: str|None=None, 헥환초기화: bool=False):
        p = {'id':entity_id(캐릭터), 'reset_hexa':헥환초기화}
        if 이름 is not None: p['name']=이름
        if 직업 is not None: p['job_id']=entity_id(직업)
        if 헥환 is not None: p['hexa']=헥환
        await self.bot.mutate(i, 'character.edit', p)

    @chars.command(name='소유자변경', description='캐릭터 소유자 정정 (관리자)')
    @app_commands.autocomplete(캐릭터=ac_own)
    async def char_owner(self, i: discord.Interaction, 캐릭터: str, 소유자: discord.Member):
        await self.bot.mutate(i, 'character.owner', {'id':entity_id(캐릭터),'owner_id':소유자.id})

    @chars.command(name='비활성화', description='배정 없는 본인 캐릭터 비활성화')
    @app_commands.autocomplete(캐릭터=ac_own)
    async def char_off(self, i: discord.Interaction, 캐릭터: str):
        await self.bot.mutate(i, 'character.deactivate', {'id':entity_id(캐릭터)})

    @chars.command(name='활성화', description='비활성 캐릭터 재활성화')
    @app_commands.autocomplete(캐릭터=ac_own)
    async def char_on(self, i: discord.Interaction, 캐릭터: str):
        await self.bot.mutate(i, 'character.activate', {'id':entity_id(캐릭터)})

    @chars.command(name='목록', description='사용자별 캐릭터 목록')
    @app_commands.choices(상태=STATES)
    async def char_list(self, i: discord.Interaction, 사용자: discord.User|None=None, 상태: str='active'):
        a = actor(i)
        rows = await asyncio.to_thread(self.bot.service.rows, a.guild_id, 'characters', actor=a, state=상태, owner_id=사용자.id if 사용자 else a.user_id)
        await send_pages(i, pages('캐릭터 목록', [safe(r['name'])+(' (비활성)' if not r['active'] else '') for r in rows]))

    @chars.command(name='조회', description='캐릭터 하나의 전체 활성 보스·동료 조회')
    @app_commands.autocomplete(캐릭터=ac_char)
    async def char_show(self, i: discord.Interaction, 캐릭터: str):
        await self.bot.char_query(i, entity_id(캐릭터))

    @app_commands.command(name='내보스', description='본인 전체 캐릭터의 파티 배정 조회')
    @app_commands.guild_only()
    async def mine(self, i: discord.Interaction):
        await self.bot.mine(i)

    @groups.command(name='생성', description='보스 아래 빈 파티 생성')
    @app_commands.autocomplete(보스=ac_boss)
    async def group_add(self, i: discord.Interaction, 보스: str, 이름: str|None=None):
        await self.bot.mutate(i, 'group.add', {'boss_id':entity_id(보스),'name':이름})

    @groups.command(name='이름변경', description='파티 표시 이름 변경')
    @app_commands.autocomplete(파티=ac_group)
    async def group_rename(self, i: discord.Interaction, 파티: str, 이름: str):
        await self.bot.mutate(i, 'group.rename', {'id':entity_id(파티),'name':이름})

    @groups.command(name='삭제', description='파티 삭제, 캐릭터 원본 보존')
    @app_commands.autocomplete(파티=ac_group)
    async def group_delete(self, i: discord.Interaction, 파티: str):
        await self.bot.mutate(i, 'group.delete', {'id':entity_id(파티)})

    @groups.command(name='멤버추가', description='파티에 활성 캐릭터 배정')
    @app_commands.autocomplete(파티=ac_group, 캐릭터=ac_char_active)
    async def group_member(self, i: discord.Interaction, 파티: str, 캐릭터: str):
        await self.bot.mutate(i, 'group.member', {'id':entity_id(파티),'character_id':entity_id(캐릭터)})

    @groups.command(name='멤버제외', description='해당 파티의 배정만 해제')
    @app_commands.autocomplete(파티=ac_group, 캐릭터=ac_char)
    async def group_remove(self, i: discord.Interaction, 파티: str, 캐릭터: str):
        await self.bot.mutate(i, 'group.remove', {'id':entity_id(파티),'character_id':entity_id(캐릭터)})

    @groups.command(name='이동', description='같은 보스의 대상 파티로 이동')
    @app_commands.autocomplete(보스=ac_boss, 캐릭터=ac_char, 대상파티=ac_group)
    async def group_move(self, i: discord.Interaction, 보스: str, 캐릭터: str, 대상파티: str):
        a = actor(i)
        bid,cid = entity_id(보스),entity_id(캐릭터)
        with self.bot.service.db.transaction(False) as c:
            row = c.execute('SELECT group_id FROM group_members WHERE guild_id=? AND boss_id=? AND character_id=?', (a.guild_id,bid,cid)).fetchone()
        if not row: raise UserError('현재 배정이 없습니다. 멤버추가를 사용해 주세요.')
        target = await asyncio.to_thread(self.bot.entity, a, 'boss_groups', entity_id(대상파티))
        if target['boss_id'] != bid: raise UserError('선택 보스의 대상 파티를 지정해 주세요.')
        await self.bot.mutate(i,'group.move',{'id':row[0],'character_id':cid,'target_id':target['id']})

    @groups.command(name='병합', description='같은 보스의 두 파티 병합, 원본 삭제')
    @app_commands.autocomplete(원본파티=ac_group, 대상파티=ac_group)
    async def group_merge(self, i: discord.Interaction, 원본파티: str, 대상파티: str):
        await self.bot.mutate(i,'group.merge',{'id':entity_id(원본파티),'target_id':entity_id(대상파티)})

    @boards.command(name='게시', description='공개 파티 배분표 게시·재개·채널 이전')
    async def board_publish(self, i: discord.Interaction, 채널: discord.TextChannel|None=None):
        await self.bot.mutate(i,'board.publish',{'channel_id':채널.id} if 채널 else {})

    @boards.command(name='중단', description='자동 갱신 중단과 기존 게시물 정리')
    async def board_stop(self, i: discord.Interaction):
        await self.bot.mutate(i,'board.stop',{})

    @boards.command(name='새로고침', description='DB 최신 상태로 갱신 재시도')
    @app_commands.autocomplete(보스=ac_boss)
    async def board_refresh(self, i: discord.Interaction, 보스: str|None=None):
        if 보스 is not None:
            await asyncio.to_thread(self.bot.entity,actor(i),'bosses',entity_id(보스))
        await self.bot.mutate(i,'board.refresh',{'boss_id':entity_id(보스)} if 보스 is not None else {})

    @manage.command(name='패널', description='개인용 파티·캐릭터 관리 화면')
    @app_commands.autocomplete(보스=ac_boss)
    async def panel(self, i: discord.Interaction, 보스: str|None=None):
        await self.bot.open_panel(i,entity_id(보스) if 보스 is not None else None)


def entity_id(value):
    try:
        result = int(value)
        if result <= 0: raise ValueError
        return result
    except (ValueError, TypeError) as e:
        raise UserError('자동완성 목록에서 항목을 선택해 주세요.') from e
