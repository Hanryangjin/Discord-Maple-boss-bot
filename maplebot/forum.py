"""Forum routing over the existing generation-fenced message reconciler."""
import asyncio
import discord
from .public_ui import parse_marker


class ForumTransportMixin:
    def forum_db(self,sql,args=(),*,write=False):
        with self.bot.service.db.transaction(write) as c:
            return [dict(r) for r in c.execute(sql,args)]

    async def remember(self,forum,thread,mid,identity=None):
        def save():
            with self.bot.service.db.transaction() as c:
                c.execute('INSERT OR IGNORE INTO board_forums VALUES(?)',(forum.id,))
                c.execute('INSERT OR REPLACE INTO forum_routes VALUES(?,?,?)',(forum.id,mid,thread.id))
                if identity:
                    c.execute('INSERT OR IGNORE INTO forum_topics VALUES(?,?,?,?,?)',(forum.id,*identity[:3],thread.id))
        await asyncio.to_thread(save)

    async def thread(self,forum,thread_id,*,writable=False):
        try:
            thread=self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
            if not isinstance(thread,discord.Thread) or thread.parent_id!=forum.id or thread.owner_id!=self.bot.user.id:
                raise PermissionError('forum thread ownership mismatch')
            if writable and (thread.archived or thread.locked):
                await thread.edit(archived=False,locked=False)
            return thread
        except discord.NotFound:
            return None
        except discord.Forbidden as error:
            raise PermissionError('forum thread forbidden') from error

    async def threads(self,forum):
        seen=set()
        # Active threads may not all be cached after reconnect.
        active=await forum.guild.active_threads()
        for thread in active:
            if thread.parent_id==forum.id and thread.owner_id==self.bot.user.id and thread.id not in seen:
                seen.add(thread.id);yield thread
        async for thread in forum.archived_threads(limit=100):
            if thread.owner_id==self.bot.user.id and thread.id not in seen:
                seen.add(thread.id);yield thread

    async def scan(self,channel_id,guild_id):
        forum=await self.channel(channel_id)
        if not isinstance(forum,discord.ForumChannel):return await super().scan(channel_id,guild_id)
        await asyncio.to_thread(self.forum_db,'INSERT OR IGNORE INTO board_forums VALUES(?)',(channel_id,),write=True)
        found=[]
        try:
            async for thread in self.threads(forum):
                records=await super().scan(thread.id,guild_id)
                for generation,boss,page,mid in records:
                    await self.remember(forum,thread,mid,(guild_id,generation,boss,page))
                    found.append((generation,boss,page,mid))
        except discord.Forbidden as error:
            raise PermissionError('forum history forbidden') from error
        return found

    async def find(self,channel_id,marker):
        forum=await self.channel(channel_id)
        if not isinstance(forum,discord.ForumChannel):return await super().find(channel_id,marker)
        identity=parse_marker(marker)
        if not identity:return None
        rows=await asyncio.to_thread(self.forum_db,
            'SELECT thread_id FROM forum_topics WHERE channel_id=? AND guild_id=? AND generation=? AND boss_id=?',
            (channel_id,*identity[:3]))
        if rows:
            thread=await self.thread(forum,rows[0]['thread_id'])
            if thread:
                mid=await super().find(thread.id,marker)
                if mid:await self.remember(forum,thread,mid,identity)
                return mid
        # Recover an uncertain create (Discord succeeded but DB persistence failed).
        records=await self.scan(channel_id,identity[0])
        matches=[mid for gen,boss,page,mid in records if (gen,boss,page)==identity[1:]]
        if matches:
            for duplicate in matches[1:]:await self.delete(channel_id,duplicate)
            return matches[0]
        return None

    async def send(self,channel_id,body,marker):
        forum=await self.channel(channel_id)
        if not isinstance(forum,discord.ForumChannel):return await super().send(channel_id,body,marker)
        identity=parse_marker(marker)
        if not identity:raise ValueError('forum publication needs identity')
        rows=await asyncio.to_thread(self.forum_db,
            'SELECT thread_id FROM forum_topics WHERE channel_id=? AND guild_id=? AND generation=? AND boss_id=?',
            (channel_id,*identity[:3]))
        thread=await self.thread(forum,rows[0]['thread_id'],writable=True) if rows else None
        if thread:
            mid=await super().send(thread.id,body,marker)
            await self.remember(forum,thread,mid,identity)
            return mid
        # Missing/deleted topic: discard only its stale routes, then create anew.
        if rows:
            await asyncio.to_thread(self.forum_db,'DELETE FROM forum_topics WHERE thread_id=?',(rows[0]['thread_id'],),write=True)
        from .party_image import prepare_parties
        from .public_ui import board_layout
        prepared=await prepare_parties(self.bot,identity[0],body.get('cards',[]),body.get('max_members',6))
        try:
            result=await forum.create_thread(name=body['title'][:100],
                view=board_layout(self.bot,body,marker,party_images=prepared.urls),
                files=prepared.files,allowed_mentions=discord.AllowedMentions.none())
            await self.remember(forum,result.thread,result.message.id,identity)
            return result.message.id
        except discord.Forbidden as error:
            raise PermissionError('forum create forbidden') from error
        finally:prepared.close()

    async def route(self,forum,mid,writable=False):
        rows=await asyncio.to_thread(self.forum_db,'SELECT thread_id FROM forum_routes WHERE channel_id=? AND message_id=?',(forum.id,mid))
        if not rows:return None
        return await self.thread(forum,rows[0]['thread_id'],writable=writable)

    async def edit(self,channel_id,mid,body,marker):
        forum=await self.channel(channel_id)
        if not isinstance(forum,discord.ForumChannel):return await super().edit(channel_id,mid,body,marker)
        thread=await self.route(forum,mid,writable=True)
        if thread is None:return False
        identity=parse_marker(marker)
        try:
            if identity and identity[3]==0 and thread.name!=body['title'][:100]:
                await thread.edit(name=body['title'][:100])
            return await super().edit(thread.id,mid,body,marker)
        except discord.Forbidden as error:
            raise PermissionError('forum edit forbidden') from error

    async def delete(self,channel_id,mid):
        try:forum=await self.channel(channel_id)
        except LookupError:return
        if not isinstance(forum,discord.ForumChannel):return await super().delete(channel_id,mid)
        thread=await self.route(forum,mid)
        if thread is None:return
        try:
            retired=await asyncio.to_thread(self.forum_db,
                "SELECT 1 FROM forum_topics t JOIN board_generations g ON g.guild_id=t.guild_id AND g.generation=t.generation WHERE t.thread_id=? AND g.status='retired'",(thread.id,))
            if mid==thread.id or retired:
                await thread.delete()
                await asyncio.to_thread(self.forum_db,'DELETE FROM forum_routes WHERE thread_id=?',(thread.id,),write=True)
                await asyncio.to_thread(self.forum_db,'DELETE FROM forum_topics WHERE thread_id=?',(thread.id,),write=True)
            else:
                await super().delete(thread.id,mid)
                await asyncio.to_thread(self.forum_db,'DELETE FROM forum_routes WHERE channel_id=? AND message_id=?',(channel_id,mid),write=True)
        except discord.NotFound:return
        except discord.Forbidden as error:raise PermissionError('forum delete forbidden') from error
