import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock,Mock
import discord
from discord.components import _component_factory
from maplebot.db import Database
from maplebot.service import Service
from maplebot.discord_app import DiscordTransport
from maplebot.party_image import prepare_parties,render_party
from maplebot.job_icons import JobIconStore
from maplebot.thumbnails import decode
from maplebot.public_ui import board_layout,message_marker
from tests.test_pagination import group,body

async def iterate(items):
    for item in items:yield item


class ForumTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Database(str(Path(self.temp.name)/'db.sqlite3'))
        self.channels={};self.next_id=1000;self.messages={}
        self.bot=SimpleNamespace(service=Service(self.db),user=SimpleNamespace(id=999),
            get_channel=lambda n:self.channels.get(n),fetch_channel=AsyncMock(side_effect=self.fetch))
        self.forum=Mock(spec=discord.ForumChannel);self.forum.id=100
        self.forum.guild=SimpleNamespace(active_threads=AsyncMock(side_effect=self.active))
        self.forum.archived_threads=lambda **kw:iterate([t for t in self.channels.values() if isinstance(t,discord.Thread) and t.archived])
        self.forum.create_thread=AsyncMock(side_effect=self.create)
        self.channels[100]=self.forum;self.transport=DiscordTransport(self.bot)
        self.data=body([group(1,2)],0,1,1);self.marker='maple:1:1:1:0'

    async def fetch(self,n):
        if n not in self.channels:raise discord.NotFound(SimpleNamespace(status=404,reason='missing'),'missing')
        return self.channels[n]

    async def active(self):return [t for t in self.channels.values() if isinstance(t,discord.Thread) and not t.archived]

    def message(self,mid,kwargs):
        msg=SimpleNamespace(id=mid,author=self.bot.user,embeds=[],components=[],images=[])
        def apply(kw):
            msg.components=[_component_factory(x) for x in kw['view'].to_components()]
            msg.images=[f.fp.read() for f in kw.get('files',kw.get('attachments',[]))]
        apply(kwargs)
        async def edit(**kw):apply(kw)
        async def delete():self.messages.pop(mid,None)
        msg.edit=AsyncMock(side_effect=edit);msg.delete=AsyncMock(side_effect=delete)
        self.messages[mid]=msg;return msg

    async def create(self,**kwargs):
        self.next_id+=1;tid=self.next_id
        t=Mock(spec=discord.Thread);t.id=tid;t.parent_id=100;t.owner_id=999;t.archived=False;t.locked=False;t.name=kwargs['name']
        t.mids=[tid]
        async def edit(**kw):
            for k,v in kw.items():setattr(t,k,v)
        async def delete():
            self.channels.pop(tid,None)
            for mid in t.mids:self.messages.pop(mid,None)
        async def fetch(mid):
            if mid not in self.messages:raise discord.NotFound(SimpleNamespace(status=404,reason='missing'),'missing')
            return self.messages[mid]
        async def send(**kw):
            self.next_id+=1;t.mids.append(self.next_id)
            return self.message(self.next_id,kw)
        t.edit=AsyncMock(side_effect=edit);t.delete=AsyncMock(side_effect=delete)
        t.send=AsyncMock(side_effect=send);t.fetch_message=AsyncMock(side_effect=fetch)
        t.history=lambda **kw:iterate([self.messages[n] for n in reversed(t.mids) if n in self.messages])
        self.channels[tid]=t
        return SimpleNamespace(thread=t,message=self.message(tid,kwargs))

    async def test_create_reuses_topic_for_more_pages_and_restart(self):
        first=await self.transport.send(100,self.data,self.marker)
        second=await self.transport.send(100,{**self.data,'page':1},'maple:1:1:1:1')
        self.forum.create_thread.assert_awaited_once()
        t=self.channels[first];t.send.assert_awaited_once()
        self.assertEqual(self.messages[first].images[0][:8],b'\x89PNG\r\n\x1a\n')
        self.assertIn('<@',str([x.to_dict() for x in self.messages[first].components]))
        restarted=DiscordTransport(self.bot)
        self.assertEqual(await restarted.find(100,self.marker),first)
        self.assertEqual(await restarted.find(100,'maple:1:1:1:1'),second)
        self.assertTrue(await restarted.edit(100,first,{**self.data,'title':'새 이름'},self.marker))
        self.assertEqual(t.name,'새 이름')
        self.assertEqual(message_marker(self.messages[first]),self.marker)

    async def test_archive_is_reopened_and_deleted_thread_is_recreated(self):
        mid=await self.transport.send(100,self.data,self.marker)
        t=self.channels[mid];t.archived=True;t.locked=True
        self.assertTrue(await self.transport.edit(100,mid,self.data,self.marker))
        self.assertFalse(t.archived);self.assertFalse(t.locked)
        self.channels.pop(mid)
        self.assertFalse(await self.transport.edit(100,mid,self.data,self.marker))
        new=await self.transport.send(100,self.data,self.marker)
        self.assertNotEqual(new,mid)

    async def test_uncertain_create_is_recovered_from_archived_history(self):
        mid=await self.transport.send(100,self.data,self.marker)
        self.channels[mid].archived=True
        with self.db.transaction() as c:
            c.execute('DELETE FROM forum_topics');c.execute('DELETE FROM forum_routes')
        self.assertEqual(await DiscordTransport(self.bot).find(100,self.marker),mid)
        self.forum.create_thread.assert_awaited_once()
        self.assertTrue(await self.transport.edit(100,mid,self.data,self.marker))

    async def test_retirement_removes_topic_and_routes(self):
        mid=await self.transport.send(100,self.data,self.marker)
        await self.transport.send(100,self.data,'maple:1:1:1:1')
        await self.transport.delete(100,mid)
        self.assertNotIn(mid,self.channels)
        await self.transport.delete(100,mid)
        with self.db.transaction(False) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM forum_topics').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM forum_routes').fetchone()[0],0)

    async def test_foreign_thread_is_never_edited(self):
        mid=await self.transport.send(100,self.data,self.marker)
        self.channels[mid].owner_id=1
        with self.assertRaises(PermissionError):await self.transport.edit(100,mid,self.data,self.marker)

    async def test_forbidden_create_does_not_save_fake_topic(self):
        self.forum.create_thread.side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='forbidden'),'forbidden')
        with self.assertRaises(PermissionError):await self.transport.send(100,self.data,self.marker)
        with self.db.transaction(False) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM forum_topics').fetchone()[0],0)

    async def test_forum_order_does_not_require_snowflake_order(self):
        from maplebot.board import BoardWorker
        with self.db.transaction() as c:
            self.db.initialize(c,1)
            c.execute("INSERT INTO board_generations VALUES(1,1,100,'active',0,NULL)")
            c.execute('INSERT INTO board_forums VALUES(100)')
            self.assertTrue(BoardWorker.physically_ordered(c,1,1))

    async def test_worker_migrates_text_board_then_updates_and_stops_forum(self):
        from maplebot.board import BoardWorker
        from maplebot.service import Actor
        actor=Actor(1,10,True)
        service=self.bot.service
        service.observe_member(1,10,'소유자','present')
        def do(action,payload):
            result=service.execute(actor,action,payload)
            return service.execute(actor,action,payload,result['token']) if result['status']=='confirm' else result
        do('boss.add',{'name':'스우','difficulty':'익스트림'})
        bid=service.rows(1,'bosses',actor=actor)[0]['id']
        do('group.add',{'boss_id':bid,'name':'첫 파티'})
        gid=service.rows(1,'boss_groups',actor=actor)[0]['id']
        text=Mock(spec=discord.TextChannel);text.id=200;text.mids=[]
        async def send(**kw):
            self.next_id+=1;text.mids.append(self.next_id)
            return self.message(self.next_id,kw)
        async def fetch(mid):
            if mid not in self.messages:raise discord.NotFound(SimpleNamespace(status=404,reason='missing'),'missing')
            return self.messages[mid]
        text.send=AsyncMock(side_effect=send);text.fetch_message=AsyncMock(side_effect=fetch)
        text.history=lambda **kw:iterate([self.messages[n] for n in reversed(text.mids) if n in self.messages])
        self.channels[200]=text
        do('settings.channel',{'value':200});do('board.publish',{})
        worker=BoardWorker(service,self.transport)
        await worker.tick()
        old=list(text.mids);self.assertTrue(old)
        do('board.publish',{'channel_id':100})
        await worker.tick()
        with self.db.transaction(False) as c:
            topic=c.execute('SELECT thread_id FROM forum_topics').fetchone()[0]
            self.assertEqual(c.execute('SELECT COUNT(*) FROM boss_groups').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT channel_id FROM board_generations WHERE status='active'").fetchone()[0],100)
        self.assertTrue(all(mid not in self.messages for mid in old))
        do('group.rename',{'id':gid,'name':'이름 수정'})
        await worker.tick()
        self.assertIn(topic,self.channels)
        self.forum.create_thread.assert_awaited_once()
        do('board.stop',{})
        await worker.tick()
        self.assertNotIn(topic,self.channels)

    async def test_forum_reorder_preserves_topic_instead_of_reposting(self):
        from maplebot.service import Actor
        service=self.bot.service;actor=Actor(1,10,True)
        def do(action,payload):
            r=service.execute(actor,action,payload)
            return service.execute(actor,action,payload,r['token']) if r['status']=='confirm' else r
        do('boss.add',{'name':'보스1','difficulty':'하드'})
        do('boss.add',{'name':'보스2','difficulty':'하드'})
        do('settings.channel',{'value':100});do('board.publish',{})
        with self.db.transaction() as c:
            c.execute('INSERT INTO board_forums VALUES(100)')
            c.execute("UPDATE board_generations SET status='active'")
            c.execute('UPDATE guild_settings SET active_generation=preparing_generation,preparing_generation=NULL')
        bid=service.rows(1,'bosses',actor=actor)[0]['id']
        do('boss.order',{'id':bid,'position':2})
        with self.db.transaction(False) as c:
            self.assertIsNone(c.execute('SELECT preparing_generation FROM guild_settings').fetchone()[0])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM board_generations').fetchone()[0],1)


class PartyPictureTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_odd_and_six_members_have_two_column_geometry(self):
        icons=JobIconStore('/tmp/no-custom-job-icons')
        for count,rows in ((0,1),(1,1),(2,1),(3,2),(6,3)):
            g=group(1,count)
            data=render_party(g,6,icons,1)
            image=decode(data)
            self.assertEqual(image.size,(1120,112+rows*170))
            if count>1:
                # Both first-row tiles exist; next row is used only after 2 members.
                self.assertEqual(image.getpixel((30,110)),image.getpixel((578,110)))
            if count%2:
                self.assertEqual(image.getpixel((1090,110+170*(rows-1))),(27,29,33,255))

    async def test_picture_changes_with_data_and_mentions_remain_outside(self):
        g=group(1,2);b=body([g],0,1,1)
        prepared=await prepare_parties(SimpleNamespace(),1,[g],6)
        try:
            view=board_layout(SimpleNamespace(),b,'maple:1:1:1:0',party_images=prepared.urls)
            serialized=str(view.to_components())
            self.assertIn('attachment://party-',serialized)
            self.assertIn('<@',serialized)
            self.assertIn('편집',serialized);self.assertIn('삭제',serialized)
            self.assertIn('관리 패널',serialized)
            old=prepared.urls.copy()
        finally:prepared.close()
        g['members'][0]['hexa_value']=75000
        result=await prepare_parties(SimpleNamespace(),1,[g],6)
        self.assertNotEqual(old,result.urls);result.close()
