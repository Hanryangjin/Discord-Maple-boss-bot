import unittest
from types import SimpleNamespace
import discord
from discord.components import _component_factory
from maplebot.cards import party_pages,validate_layout
from maplebot.public_ui import board_layout,message_marker


def group(gid,count):
    return {'id':gid,'name':f'품앗이{gid}', 'members':[
        {'id':gid*10+n,'name':f'캐릭터{gid}{n}','job_name':'렌','job_active':True,
         'hexa_value':72000,'owner_id':100+n,'membership':'present','display_name':'소유자'}
        for n in range(count)]}


def body(batch,index,total,group_count):
    return {'title':'스우 (익스트림)','description':'','cards':batch,'max_members':6,
            'page':index,'page_count':total,'group_count':group_count}


class PublicPaginationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot=SimpleNamespace(get_guild=lambda _:None,get_user=lambda _:None)

    async def test_three_two_member_parties_fit_in_one_public_message(self):
        groups=[group(n,2) for n in range(1,4)]
        private=party_pages(groups)
        public=party_pages(groups,component_budget=36,text_budget=3400)
        self.assertEqual(len(private),2)
        self.assertEqual(len(public),1)
        view=board_layout(self.bot,body(public[0],0,1,3),'maple:1:1:1:0')
        self.assertEqual(view.total_children_count,40)
        validate_layout(view)
        text='\n'.join(i.content for i in view.walk_children() if isinstance(i,discord.ui.TextDisplay))
        self.assertNotIn('페이지',text)
        for n in range(1,4):self.assertIn(f'품앗이{n}',text)

    async def test_hard_limit_uses_small_continuation_and_footer_only_at_end(self):
        groups=[group(n,6) for n in range(1,4)]
        batches=party_pages(groups,component_budget=36,text_budget=3400)
        self.assertEqual(len(batches),3)
        seen=[]
        for index,batch in enumerate(batches):
            marker=f'maple:1:1:1:{index}'
            view=board_layout(self.bot,body(batch,index,len(batches),len(groups)),marker)
            validate_layout(view)
            text=[i.content for i in view.walk_children() if isinstance(i,discord.ui.TextDisplay)]
            if index==0:self.assertTrue(text[0].startswith('# '))
            else:
                self.assertTrue(text[0].startswith('-# '))
                self.assertIn('계속',text[0])
            self.assertNotIn('페이지','\n'.join(text))
            buttons=[getattr(i,'item',i) for i in view.walk_children()]
            labels=[i.label for i in buttons if isinstance(i,discord.ui.Button)]
            self.assertEqual('관리 패널' in labels,index==len(batches)-1)
            message=SimpleNamespace(embeds=[],components=[_component_factory(d) for d in view.to_components()])
            self.assertEqual(message_marker(message),marker)
            seen.extend(g['id'] for g in batch)
        self.assertEqual(seen,[1,2,3])

    async def test_empty_parties_use_actual_component_count(self):
        groups=[group(n,0) for n in range(1,7)]
        batches=party_pages(groups,component_budget=36,text_budget=3400)
        self.assertEqual([len(batch) for batch in batches],[5,1])
        for index,batch in enumerate(batches):
            validate_layout(board_layout(self.bot,body(batch,index,len(batches),len(groups)),f'maple:1:1:1:{index}'))

    async def test_long_names_split_before_text_limit(self):
        groups=[group(n,2) for n in range(1,15)]
        for g in groups:
            g['name']='_'*50
            for member in g['members']:
                member['job_name']='_'*50
                member['display_name']='_'*32
                member['membership']='unknown'
        batches=party_pages(groups,component_budget=36,text_budget=3400)
        for index,batch in enumerate(batches):
            validate_layout(board_layout(self.bot,body(batch,index,len(batches),len(groups)),f'maple:1:1:1:{index}'))
        self.assertEqual(sum(map(len,batches)),len(groups))
