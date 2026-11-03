import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import discord
from discord.components import _component_factory
from maplebot.db import Database
from maplebot.discord_app import MapleBot, DiscordTransport
from maplebot.service import Actor
from maplebot.panel import PanelSession, route
from maplebot.public_ui import board_layout, message_marker, PublicAction, PATTERN
from maplebot.render import boss_pages
from maplebot.cards import validate_layout
import re


class Response:
    def __init__(self):
        self.done=False
        self.deferrals=[]
        self.modal=None
        self.send_message=AsyncMock(side_effect=self.sent)

    async def sent(self,*args,**kwargs):self.done=True
    def is_done(self):return self.done
    async def defer(self,**kwargs):
        if self.done:raise AssertionError('interaction already acknowledged')
        self.done=True;self.deferrals.append(kwargs)
    async def send_modal(self,modal):
        if self.done:raise AssertionError('modal after acknowledgement')
        self.done=True;self.modal=modal


class Interaction:
    def __init__(self,bot,root,user_id=10,admin=True,data=None):
        self.user=Mock(spec=discord.Member)
        self.user.id=user_id
        self.user.roles=[]
        self.user.guild_permissions=SimpleNamespace(administrator=admin)
        self.guild_id=1
        self.guild=SimpleNamespace(id=1)
        self.client=bot
        self.response=Response()
        self.followup=SimpleNamespace(send=AsyncMock())
        self.root=root
        self.message=root
        self.data=data or {}
        self.edit_original_response=AsyncMock(side_effect=self.edited)
        self.original_response=AsyncMock(return_value=root)
        self.delete_original_response=AsyncMock(side_effect=root.delete)

    async def edited(self,**kwargs):
        self.root.edits.append(kwargs)
        self.root.view=kwargs.get('view')
        return self.root


def button(view,label):
    return next(item for item in view.walk_children() if isinstance(item,discord.ui.Button) and item.label==label)


class UIIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.bot=MapleBot(Database(str(Path(self.tmp.name)/'db.sqlite3')))
        self.bot.observe=AsyncMock()  # Members are already verified in the fixture.
        from tests.test_thumbnails import png
        self.bot.thumbnails.reader=AsyncMock(return_value=png('blue'))
        self.service=self.bot.service
        self.a=Actor(1,10,True)
        for uid in range(10,30):self.service.observe_member(1,uid,f'user{uid}','present')
        self.job=self.service.rows(1,'jobs',actor=self.a)[0]['id']
        self.do('boss.add',{'name':'스우','difficulty':'익스트림'})
        self.bid=self.service.rows(1,'bosses',actor=self.a)[0]['id']
        self.do('group.add',{'boss_id':self.bid,'name':'첫 파티'})
        self.gid=self.service.rows(1,'boss_groups',actor=self.a)[0]['id']
        self.root=SimpleNamespace(edits=[],view=None,edit=AsyncMock(),delete=AsyncMock())

    async def asyncTearDown(self):await self.bot.close()

    def do(self,action,p):
        r=self.service.execute(self.a,action,p)
        return self.service.execute(self.a,action,p,r['token']) if r['status']=='confirm' else r

    def char(self,n,owner):
        name=f'캐릭터{n}'
        self.do('character.add',{'name':name,'job_id':self.job,'owner_id':owner,'hexa':'5.25'})
        return next(r['id'] for r in self.service.rows(1,'characters',actor=self.a,state='all') if r['name']==name)

    def i(self,**kwargs):return Interaction(self.bot,self.root,**kwargs)

    async def click(self,session,label,**kwargs):
        i=self.i(**kwargs)
        await button(session.view,label).callback(i)
        i.followup.send.assert_not_awaited()
        i.response.send_message.assert_not_awaited()
        return i

    async def test_navigation_mutation_and_confirmation_edit_one_root(self):
        session=PanelSession(self.bot,10,1,route('boss',boss_id=self.bid))
        first=self.i();await session.start(first)
        await self.click(session,'편집')
        self.assertEqual(session.current['kind'],'party')
        modal_i=await self.click(session,'이름 변경')
        self.assertIsNotNone(modal_i.response.modal)
        modal_i.response.modal.inputs[0]._value='이름 바꿈'
        submitted=self.i()
        await modal_i.response.modal.on_submit(submitted)
        self.assertEqual(session.current['kind'],'party')
        self.assertIn('이름 바꿈',str(session.view.to_components()))
        submitted.followup.send.assert_not_awaited()
        await self.click(session,'파티 삭제')
        self.assertEqual(session.current['kind'],'confirm')
        await self.click(session,'취소')
        self.assertEqual(session.current['kind'],'boss')
        await self.click(session,'편집')
        await self.click(session,'파티 삭제')
        await self.click(session,'확인하고 실행')
        self.assertEqual(session.current['kind'],'boss')
        self.assertIn('파티가 없습니다',str(session.view.to_components()))
        self.assertTrue(all(edit['content'] is None and edit['embeds']==[] for edit in self.root.edits))
        self.assertEqual(len(first.response.deferrals),1)

    async def test_selection_search_modal_returns_to_same_screen(self):
        session=PanelSession(self.bot,10,1)
        await session.start(self.i())
        await self.click(session,'조회')
        i=await self.click(session,'이름 검색')
        i.response.modal.inputs[0]._value='찾을수없음'
        j=self.i();await i.response.modal.on_submit(j)
        self.assertEqual(session.current['query'],'찾을수없음')
        self.assertIn('선택 가능한 항목이 없습니다',str(session.view.to_components()))
        j.followup.send.assert_not_awaited()
        await self.click(session,'뒤로')
        self.assertEqual(session.current['kind'],'home')

    async def test_six_avatars_and_page_navigation_stay_in_limits(self):
        for n in range(6):
            cid=self.char(n,10+n)
            self.do('group.member',{'id':self.gid,'character_id':cid})
        self.do('group.add',{'boss_id':self.bid,'name':'둘째 파티'})
        session=PanelSession(self.bot,10,1,route('boss',boss_id=self.bid))
        session.notice='오류 또는 완료 안내'
        await session.start(self.i())
        self.assertLessEqual(session.view.total_children_count,40)
        thumbs=[x for x in session.view.walk_children() if isinstance(x,discord.ui.Thumbnail)]
        self.assertEqual(len(thumbs),6)
        self.assertIn('첫 파티',str(session.view.to_components()))
        await self.click(session,'다음 파티')
        self.assertIn('둘째 파티',str(session.view.to_components()))
        await self.click(session,'이전 파티')
        await self.click(session,'편집')
        self.assertLessEqual(session.view.total_children_count,40)
        validate_layout(session.view)

    async def test_permission_revoked_and_stale_modal_do_not_mutate(self):
        cid=self.char(1,10)
        session=PanelSession(self.bot,10,1,route('character',char_id=cid))
        await session.start(self.i())
        pending=await self.click(session,'이름·헥환 수정')
        pending.response.modal.inputs[0]._value='바뀌면안됨'
        await self.click(session,'관리 홈')
        await pending.response.modal.on_submit(self.i())
        self.assertIn('이전 화면',str(session.view.to_components()))
        self.assertEqual(self.service.rows(1,'characters',actor=self.a)[0]['name'],'캐릭터1')
        denied=await self.click(session,'캐릭터 관리',admin=False)
        self.assertFalse(session.active)
        self.assertIn('관리 화면 종료',str(denied.root.view.to_components()))
        self.assertNotIn('캐릭터1',str(denied.root.view.to_components()))

    async def test_timeout_and_close_replace_current_message(self):
        session=PanelSession(self.bot,10,1)
        await session.start(self.i())
        await session.view.on_timeout()
        self.assertFalse(session.active)
        self.root.edit.assert_awaited_once()
        self.assertIn('만료',str(self.root.edit.call_args.kwargs['view'].to_components()))
        session=PanelSession(self.bot,10,1)
        await session.start(self.i())
        closed=await self.click(session,'닫기')
        self.assertFalse(session.active)
        closed.delete_original_response.assert_awaited_once()

    async def test_public_v2_cards_and_legacy_marker_migration(self):
        cid=self.char(1,10);self.do('group.member',{'id':self.gid,'character_id':cid})
        with self.service.db.transaction(False) as c:content=boss_pages(c,1,self.bid)
        marker=f'maple:1:2:{self.bid}:0'
        layout=board_layout(self.bot,content[0],marker)
        self.assertTrue(layout.is_persistent())
        payload=layout.to_components()
        self.assertEqual(content[0]['title'],'스우 (익스트림)')
        self.assertIn('스우',str(payload));self.assertIn('첫 파티',str(payload))
        self.assertIn('5.25만',str(payload))
        visible=[x.content for x in layout.walk_children() if isinstance(x,discord.ui.TextDisplay)]
        self.assertNotIn(marker,'\n'.join(visible))
        self.assertNotIn('파티 배분표','\n'.join(visible))
        self.assertNotIn('maple:','\n'.join(visible))
        message=SimpleNamespace(embeds=[],components=[_component_factory(d) for d in payload])
        self.assertEqual(message_marker(message),marker)
        legacy=discord.Embed(title='old');legacy.set_footer(text=marker)
        self.assertEqual(message_marker(SimpleNamespace(embeds=[legacy],components=[])),marker)
        edit=next(x for x in layout.walk_children() if isinstance(x,PublicAction) and x.verb=='edit')
        reconstructed=await PublicAction.from_custom_id(None,edit.item,re.fullmatch(PATTERN,edit.custom_id))
        self.assertEqual((reconstructed.guild_id,reconstructed.boss_id,reconstructed.group_id),(1,self.bid,self.gid))
        self.assertLessEqual(len(edit.custom_id),100)
        large=PublicAction(*([2**63-1]*4),'edit',2**63-1)
        self.assertLessEqual(len(large.custom_id),100)

    async def test_transport_edit_clears_embed_for_v2_upgrade(self):
        marker=f'maple:1:2:{self.bid}:0'
        message=SimpleNamespace(author=SimpleNamespace(id=999),edit=AsyncMock())
        channel=SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        transport=DiscordTransport(SimpleNamespace(user=SimpleNamespace(id=999),get_guild=lambda g:None,get_user=lambda u:None))
        transport.channel=AsyncMock(return_value=channel)
        with self.service.db.transaction(False) as c:body=boss_pages(c,1,self.bid)[0]
        self.assertTrue(await transport.edit(100,101,body,marker))
        kwargs=message.edit.call_args.kwargs
        self.assertEqual(kwargs['embeds'],[])
        self.assertIsNone(kwargs['content'])
        self.assertIsInstance(kwargs['view'],discord.ui.LayoutView)

    async def test_explicit_reopen_replaces_previous_session(self):
        first=self.i();await self.bot.open_panel(first,boss_id=self.bid)
        previous=self.bot.panel_sessions[(1,10)]
        second=self.i();await self.bot.open_panel(second)
        self.assertFalse(previous.active)
        self.root.delete.assert_awaited_once()
        self.assertIsNot(previous,self.bot.panel_sessions[(1,10)])
        first.followup.send.assert_not_awaited();second.followup.send.assert_not_awaited()

    async def test_character_registration_user_job_modal_confirm_and_updated_detail(self):
        session=PanelSession(self.bot,10,1)
        await session.start(self.i())
        await self.click(session,'＋ 캐릭터 등록')
        selector=next(x for x in session.view.walk_children() if isinstance(x,discord.ui.UserSelect))
        i=self.i(data={'values':['11']});await selector.callback(i)
        selector=next(x for x in session.view.walk_children() if isinstance(x,discord.ui.Select))
        i=self.i(data={'values':[str(self.job)]});await selector.callback(i)
        form_i=await self.click(session,'등록 정보 입력')
        form_i.response.modal.inputs[0]._value='대리등록'
        form_i.response.modal.inputs[1]._value='7.20'
        submitted=self.i();await form_i.response.modal.on_submit(submitted)
        self.assertEqual(session.current['kind'],'confirm')
        self.assertIn('대리등록',str(session.view.to_components()))
        await self.click(session,'확인하고 실행')
        self.assertEqual(session.current['kind'],'characters')
        selector=next(x for x in session.view.walk_children() if isinstance(x,discord.ui.Select))
        cid=next(r['id'] for r in self.service.rows(1,'characters',actor=self.a) if r['name']=='대리등록')
        i=self.i(data={'values':[str(cid)]});await selector.callback(i)
        self.assertIn('7.20만',str(session.view.to_components()))
        await self.click(session,'헥환 초기화')
        self.assertIn('미등록',str(session.view.to_components()))
        for interaction in (i,submitted,form_i):
            interaction.followup.send.assert_not_awaited()
            interaction.response.send_message.assert_not_awaited()

    async def test_double_confirmation_and_other_user_are_rejected(self):
        session=PanelSession(self.bot,10,1,route('party',boss_id=self.bid,group_id=self.gid))
        await session.start(self.i())
        self.assertFalse(await session.view.interaction_check(self.i(user_id=11)))
        await self.click(session,'파티 삭제')
        old_button=button(session.view,'확인하고 실행')
        one,two=self.i(),self.i()
        import asyncio
        await asyncio.gather(old_button.callback(one),old_button.callback(two))
        with self.service.db.transaction(False) as c:
            count=c.execute("SELECT COUNT(*) FROM audit_logs WHERE action='group.delete'").fetchone()[0]
        self.assertEqual(count,1)
        one.followup.send.assert_not_awaited();two.followup.send.assert_not_awaited()

    async def test_dynamic_public_actions_recheck_generation_and_route_to_private_panel(self):
        self.do('settings.channel',{'value':100})
        self.do('board.publish',{})
        from maplebot.board import BoardWorker
        from tests.test_board import FakeDiscord
        worker=BoardWorker(self.service,FakeDiscord())
        await worker.tick()
        i=self.i()
        i.message=SimpleNamespace(author=SimpleNamespace(id=999))
        i.client=SimpleNamespace(user=SimpleNamespace(id=999),service=self.service,open_panel=AsyncMock(),mine=AsyncMock())
        action=PublicAction(1,1,self.bid,0,'edit',self.gid)
        await action.callback(i)
        i.client.open_panel.assert_awaited_once_with(i,boss_id=self.bid,group_id=self.gid,delete_group=False)
        self.do('board.stop',{})
        expired=self.i();expired.message=i.message;expired.client=i.client
        await action.callback(expired)
        expired.response.send_message.assert_awaited_once()
        self.assertIn('중단',expired.response.send_message.call_args.args[0])
        self.assertEqual(i.client.open_panel.await_count,1)

    async def test_card_pagination_with_long_names_and_many_bosses(self):
        # Multi-page selectors must not lose items above Discord's 25-option limit.
        for n in range(26):self.do('boss.add',{'name':f'추가보스{n}','difficulty':'하드'})
        session=PanelSession(self.bot,10,1,route('boss',boss_id=self.bid))
        await session.start(self.i())
        await self.click(session,'보스 검색·선택')
        selector=next(x for x in session.view.walk_children() if isinstance(x,discord.ui.Select))
        self.assertEqual(len(selector.options),25)
        await self.click(session,'다음 페이지')
        selector=next(x for x in session.view.walk_children() if isinstance(x,discord.ui.Select))
        self.assertEqual(len(selector.options),2)
        validate_layout(session.view)

    async def test_transport_recovers_v2_and_legacy_metadata_but_ignores_other_authors(self):
        marker=f'maple:1:2:{self.bid}:0'
        with self.service.db.transaction(False) as c:body=boss_pages(c,1,self.bid)[0]
        view=board_layout(self.bot,body,marker)
        modern=SimpleNamespace(id=101,author=SimpleNamespace(id=999),embeds=[],
            components=[_component_factory(item) for item in view.to_components()])
        embed=discord.Embed(title='legacy');embed.set_footer(text=marker)
        legacy=SimpleNamespace(id=102,author=SimpleNamespace(id=999),embeds=[embed],components=[])
        forged=SimpleNamespace(id=103,author=SimpleNamespace(id=111),embeds=[embed],components=[])
        class Channel:
            async def history(self,limit):
                for message in (modern,legacy,forged):yield message
        transport=DiscordTransport(SimpleNamespace(user=SimpleNamespace(id=999)))
        transport.channel=AsyncMock(return_value=Channel())
        found=await transport.scan(100,1)
        self.assertEqual(found,[(2,self.bid,0,101),(2,self.bid,0,102)])
        transport.delete=AsyncMock()
        self.assertEqual(await transport.find(100,marker),101)
        transport.delete.assert_awaited_once_with(100,102)

    async def test_boss_order_screen_moves_and_confirms_in_the_same_root(self):
        self.do('boss.add',{'name':'다른보스','difficulty':'하드'})
        other=next(r['id'] for r in self.service.rows(1,'bosses',actor=self.a) if r['name']=='다른보스')
        session=PanelSession(self.bot,10,1,route('boss',boss_id=other))
        await session.start(self.i())
        await self.click(session,'보스 순서')
        self.assertEqual(session.current['kind'],'boss_order')
        await self.click(session,'위로')
        self.assertEqual(session.current['kind'],'confirm')
        self.assertIn('기존 메시지 링크',str(session.view.to_components()))
        await self.click(session,'확인하고 실행')
        self.assertEqual(session.current['kind'],'boss_order')
        self.assertEqual(self.service.rows(1,'bosses',actor=self.a)[0]['id'],other)
        self.assertTrue(button(session.view,'위로').disabled)
        form=await self.click(session,'위치 직접 지정')
        form.response.modal.inputs[0]._value='2'
        submitted=self.i();await form.response.modal.on_submit(submitted)
        await self.click(session,'확인하고 실행')
        self.assertEqual(self.service.rows(1,'bosses',actor=self.a)[1]['id'],other)
        self.assertTrue(all(update['content'] is None for update in self.root.edits))
        submitted.followup.send.assert_not_awaited()

    async def test_boss_order_buttons_disabled_during_publication(self):
        self.do('settings.channel',{'value':100})
        self.do('board.publish',{})
        session=PanelSession(self.bot,10,1,route('boss_order',boss_id=self.bid))
        await session.start(self.i())
        for label in ('위로','아래로','위치 직접 지정'):
            self.assertTrue(button(session.view,label).disabled)
        self.assertIn('반영·이전 작업 중',str(session.view.to_components()))
