import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import discord
from unittest.mock import AsyncMock

from PIL import Image
from maplebot.thumbnails import ThumbnailComposer, compose, decode, MAX_BYTES


def png(color, size=(64,64)):
    out=io.BytesIO()
    Image.new('RGBA',size,color).save(out,format='PNG')
    return out.getvalue()


class Asset:
    def __init__(self,url):self.url=url
    def replace(self,**kwargs):return self
    def __str__(self):return self.url


def fake_bot(urls=None):
    urls=urls or {10:'https://cdn.discordapp.com/avatars/10/first.png'}
    return SimpleNamespace(get_guild=lambda _:None,get_user=lambda uid:SimpleNamespace(display_avatar=Asset(urls[uid])) if uid in urls else None)


class CompositionTests(unittest.TestCase):
    def test_relative_sizes_positions_and_transparency(self):
        image=decode(compose(png('red'),png('blue')))
        self.assertEqual(image.size,(200,128))
        self.assertEqual(image.getpixel((20,80)),(255,0,0,255))  # Small character on left.
        self.assertEqual(image.getpixel((136,64)),(0,0,255,255))  # Large avatar on right.
        self.assertEqual(image.getpixel((0,0))[3],0)
        self.assertEqual(image.getpixel((80,8))[3],0)  # Circular avatar corner.
        red=sum(1 for y in range(128) for x in range(200) if image.getpixel((x,y))==(255,0,0,255))
        blue=sum(1 for y in range(128) for x in range(200) if image.getpixel((x,y))==(0,0,255,255))
        self.assertGreater(blue,red*2)

    def test_corrupt_avatar_has_placeholder_and_corrupt_icon_is_rejected(self):
        self.assertEqual(decode(compose(png('red'),b'not an avatar')).size,(200,128))
        self.assertEqual(decode(compose(png('red'),None)).size,(200,128))
        with self.assertRaises(OSError):compose(b'broken',png('blue'))

    def test_image_size_limits(self):
        with self.assertRaises(ValueError):decode(b'x'*(MAX_BYTES+1))
        with self.assertRaises(ValueError):decode(png('red',(2100,2100)))



class ComposerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        (self.root/'1').mkdir()
        self.icon=self.root/'1'/'1.png';self.icon.write_bytes(png('red'))
        self.member={'id':1,'name':'캐릭터','owner_id':10,'job_id':1,'job_icon_key':None}
        self.urls={10:'https://cdn.discordapp.com/avatars/10/first.png'}
        self.reader=AsyncMock(return_value=png('blue'))
        self.composer=ThumbnailComposer(fake_bot(self.urls),self.root,reader=self.reader)

    async def asyncTearDown(self):await self.composer.close()

    async def test_prepare_attaches_png_and_reuses_cache_with_fresh_files(self):
        first=await self.composer.prepare(1,[self.member])
        self.assertTrue(first.urls[1].startswith('attachment://character-1-'))
        self.assertEqual(decode(first.files[0].fp.read()).size,(200,128))
        first.close()
        second=await self.composer.prepare(1,[self.member])
        self.assertEqual(first.urls,second.urls)
        self.assertEqual(len(second.files),1)
        self.assertFalse(second.files[0].fp.closed)
        self.assertIsNot(first.files[0],second.files[0])
        second.close()
        self.reader.assert_awaited_once()
        self.assertEqual(len(self.composer.image_cache),1)

    async def test_avatar_change_and_icon_change_invalidate_output(self):
        first=await self.composer.prepare(1,[self.member]);first.close()
        self.urls[10]='https://cdn.discordapp.com/avatars/10/second.png'
        self.reader.return_value=png('green')
        second=await self.composer.prepare(1,[self.member]);second.close()
        self.assertNotEqual(first.urls,second.urls)
        self.icon.write_bytes(png('yellow'))
        third=await self.composer.prepare(1,[self.member]);third.close()
        self.assertNotEqual(second.urls,third.urls)
        self.assertEqual(self.reader.await_count,2)

    async def test_missing_icon_uses_common_placeholder_with_avatar(self):
        self.icon.unlink()
        result=await self.composer.prepare(1,[self.member])
        self.assertIn(1,result.urls)
        self.assertEqual(len(result.files),1)
        self.reader.assert_awaited_once()
        result.close()

    async def test_corrupt_icon_and_failed_download_do_not_break_render(self):
        self.icon.write_bytes(b'broken')
        result=await self.composer.prepare(1,[self.member])
        self.assertEqual(len(result.files),1)
        result.close()
        self.icon.write_bytes(png('red'))
        self.composer.avatar_cache.clear()
        self.reader.side_effect=OSError('offline')
        result=await self.composer.prepare(1,[self.member])
        self.assertEqual(len(result.files),1)
        self.assertEqual(decode(result.files[0].fp.read()).size,(200,128))
        result.close()

    async def test_concurrent_characters_share_avatar_fetch(self):
        (self.root/'1'/'2.png').write_bytes(png('green'))
        async def slow(url):
            await asyncio.sleep(0.01)
            return png('blue')
        self.reader.side_effect=slow
        result=await self.composer.prepare(1,[self.member,{'id':2,'name':'다른캐릭터','owner_id':10,'job_id':2,'job_icon_key':None}])
        self.assertEqual(len(result.files),2)
        self.reader.assert_awaited_once()
        result.close()

    async def test_failure_cache_retries_after_expiration(self):
        self.reader.side_effect=OSError('offline')
        one=await self.composer.prepare(1,[self.member]);one.close()
        two=await self.composer.prepare(1,[self.member]);two.close()
        self.reader.assert_awaited_once()
        self.composer.avatar_cache.clear()
        self.reader.side_effect=None
        self.reader.return_value=png('blue')
        three=await self.composer.prepare(1,[self.member]);three.close()
        self.assertNotEqual(two.urls,three.urls)

    async def test_attachment_limit_and_deduplication(self):
        with self.assertRaises(ValueError):
            await self.composer.prepare(1,[{**self.member,'id':n} for n in range(11)])
        result=await self.composer.prepare(1,[self.member,self.member])
        self.assertEqual(len(result.files),1)
        result.close()

class MediaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from maplebot.db import Database
        from maplebot.discord_app import MapleBot
        from maplebot.service import Actor
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.bot=MapleBot(Database(str(self.root/'db.sqlite3')))
        self.bot.observe=AsyncMock()
        self.reader=AsyncMock(return_value=png('blue'))
        self.bot.thumbnails=ThumbnailComposer(self.bot,self.root/'icons',reader=self.reader)
        self.bot.thumbnails.root.mkdir()
        # Built-in job assets or the common placeholder are selected by job ID.
        self.a=Actor(1,10,True)
        self.bot.service.observe_member(1,10,'user','present')
        job=self.bot.service.rows(1,'jobs',actor=self.a)[0]['id']
        self.do('character.add',{'name':'대상캐릭','job_id':job,'owner_id':10})
        self.cid=self.bot.service.rows(1,'characters',actor=self.a)[0]['id']
        self.do('boss.add',{'name':'보스','difficulty':'하드'})
        self.bid=self.bot.service.rows(1,'bosses',actor=self.a)[0]['id']
        self.do('group.add',{'boss_id':self.bid})
        self.gid=self.bot.service.rows(1,'boss_groups',actor=self.a)[0]['id']
        self.do('group.member',{'id':self.gid,'character_id':self.cid})

    async def asyncTearDown(self):await self.bot.close()

    def do(self,action,payload):
        result=self.bot.service.execute(self.a,action,payload)
        if result['status']=='confirm':result=self.bot.service.execute(self.a,action,payload,result['token'])
        return result

    async def test_public_send_edit_and_retirement_manage_attachments(self):
        from maplebot.discord_app import DiscordTransport
        from maplebot.render import boss_pages
        with self.bot.service.db.transaction(False) as c:body=boss_pages(c,1,self.bid)[0]
        records=[]
        async def capture(**kwargs):
            files=kwargs.get('files',kwargs.get('attachments',[]))
            records.append({'files':len(files),'images':[f.fp.read() for f in files],'components':str(kwargs['view'].to_components())})
            return SimpleNamespace(id=101)
        message=SimpleNamespace(author=SimpleNamespace(id=999),edit=AsyncMock(side_effect=capture))
        channel=SimpleNamespace(send=AsyncMock(side_effect=capture),fetch_message=AsyncMock(return_value=message))
        transport=DiscordTransport(SimpleNamespace(user=SimpleNamespace(id=999),thumbnails=self.bot.thumbnails,
            get_guild=self.bot.get_guild,get_user=self.bot.get_user))
        transport.channel=AsyncMock(return_value=channel)
        marker=f'maple:1:1:{self.bid}:0'
        self.assertEqual(await transport.send(100,body,marker),101)
        self.assertTrue(await transport.edit(100,101,body,marker))
        self.assertEqual([r['files'] for r in records],[1,1])
        self.assertTrue(all('attachment://character-' in r['components'] for r in records))
        self.assertEqual(decode(records[0]['images'][0]).size,(200,128))
        await transport.edit(100,101,{'title':'운영 종료','description':'중단되었습니다.'},'maple:retired')
        self.assertEqual(records[-1]['files'],0)
        self.assertEqual(message.edit.call_args.kwargs['attachments'],[])
        self.reader.assert_awaited_once()

    async def test_private_panel_uses_one_message_and_removes_old_images_on_navigation(self):
        from maplebot.panel import PanelSession,route
        from tests.test_ui import Interaction,button
        root=SimpleNamespace(edits=[],view=None,edit=AsyncMock(),delete=AsyncMock())
        uploaded=[]
        i=Interaction(self.bot,root)
        original=i.edit_original_response.side_effect
        async def capture(**kwargs):
            uploaded.extend(f.fp.read() for f in kwargs.get('attachments',[]))
            return await original(**kwargs)
        i.edit_original_response.side_effect=capture
        session=PanelSession(self.bot,10,1,route('boss',boss_id=self.bid))
        await session.start(i)
        self.assertEqual(len(uploaded),1)
        self.assertIn('attachment://character-',str(session.view.to_components()))
        self.assertEqual(decode(uploaded[0]).size,(200,128))
        j=Interaction(self.bot,root)
        await button(session.view,'관리 홈').callback(j)
        self.assertEqual(j.edit_original_response.call_args.kwargs['attachments'],[])
        self.assertNotIn('attachment://',str(session.view.to_components()))
        i.followup.send.assert_not_awaited();j.followup.send.assert_not_awaited()

    async def test_avatar_event_marks_existing_board_for_refresh(self):
        self.do('settings.channel',{'value':100})
        self.do('board.publish',{})
        with self.bot.service.db.transaction(False) as c:
            before=c.execute('SELECT requested FROM board_sync_state WHERE boss_id=?',(self.bid,)).fetchone()[0]
        await self.bot.on_user_update(SimpleNamespace(display_avatar='before'),SimpleNamespace(id=10,display_avatar='after'))
        with self.bot.service.db.transaction(False) as c:
            after=c.execute('SELECT requested FROM board_sync_state WHERE boss_id=?',(self.bid,)).fetchone()[0]
        self.assertGreater(after,before)

    async def test_discord_multipart_payload_contains_matching_image_attachment(self):
        import json
        from discord.http import handle_message_parameters
        from maplebot.public_ui import board_layout
        from maplebot.render import boss_pages
        with self.bot.service.db.transaction(False) as c:body=boss_pages(c,1,self.bid)[0]
        members=[m for g in body['cards'] for m in g['members']]
        prepared=await self.bot.thumbnails.prepare(1,members)
        view=board_layout(self.bot,body,f'maple:1:1:{self.bid}:0',prepared.urls)
        with handle_message_parameters(view=view,files=prepared.files,allowed_mentions=discord.AllowedMentions.none()) as params:
            payload=json.loads(next(part['value'] for part in params.multipart if part['name']=='payload_json'))
            self.assertEqual(len(payload['attachments']),1)
            name=payload['attachments'][0]['filename']
            self.assertIn('attachment://'+name,str(payload['components']))
            self.assertTrue(payload['flags'] & (1 << 15))
        prepared.close()
