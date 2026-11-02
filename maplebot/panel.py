"""Private management UI, sharing the command application's service methods."""
import asyncio
import discord
from .discord_app import PrivateView, actor, report
from .validation import UserError, show_hexa
from .render import safe


class TextForm(discord.ui.Modal):
    def __init__(self, title, fields, callback):
        super().__init__(title=title, timeout=300)
        self.callback_fn = callback
        self.inputs = []
        for name, required, default in fields:
            item = discord.ui.TextInput(label=name, required=required, default=default, max_length=100)
            self.inputs.append(item)
            self.add_item(item)

    async def on_submit(self, i):
        try:
            await self.callback_fn(i, [str(item.value) for item in self.inputs])
        except Exception as e:
            await report(i, e)

    async def on_error(self, i, error):
        await report(i, error)


class Picker(PrivateView):
    def __init__(self, user_id, rows, callback, *, label='항목 선택', page=0):
        super().__init__(user_id)
        self.rows, self.callback_fn, self.page = rows, callback, page
        options = []
        for row in rows[page*25:page*25+25]:
            options.append(discord.SelectOption(label=row.get('label', row['name'])[:100], value=str(row['id'])))
        if options:
            select = discord.ui.Select(placeholder=label, options=options)
            select.callback = self.selected
            self.add_item(select)
        previous = discord.ui.Button(label='이전', disabled=page==0)
        next_button = discord.ui.Button(label='다음', disabled=(page+1)*25>=len(rows))
        previous.callback = self.previous
        next_button.callback = self.next
        self.add_item(previous)
        self.add_item(next_button)
        search = discord.ui.Button(label='이름 검색')
        search.callback = self.search
        self.add_item(search)

    async def selected(self, i):
        await self.callback_fn(i, int(i.data['values'][0]))

    async def previous(self, i):
        await i.response.edit_message(view=Picker(self.user_id,self.rows,self.callback_fn,page=self.page-1))

    async def next(self, i):
        await i.response.edit_message(view=Picker(self.user_id,self.rows,self.callback_fn,page=self.page+1))

    async def search(self, i):
        async def submit(j, values):
            from .validation import key
            found = [r for r in self.rows if key(values[0]) in key(r.get('label',r['name']))]
            await j.response.send_message('검색 결과' if found else '검색 결과가 없습니다. 이전 화면에서 다시 검색해 주세요.',
                                          view=Picker(self.user_id,found,self.callback_fn),ephemeral=True)
        await i.response.send_modal(TextForm('이름 검색',[('검색어',True,None)],submit))


async def pick(i, rows, callback, text='항목을 선택해 주세요.'):
    await i.response.send_message(text, view=Picker(i.user.id, rows, callback), ephemeral=True)


async def rows(bot, i, table, **kw):
    a = actor(i)
    await asyncio.to_thread(bot.service.settings, a)  # Runtime permissions on every panel operation.
    return await asyncio.to_thread(bot.service.rows,a.guild_id,table,actor=a,**kw)


class UserPicker(PrivateView):
    def __init__(self, user_id, callback):
        super().__init__(user_id)
        self.callback_fn = callback
        select = discord.ui.UserSelect(placeholder='소유자 선택')
        select.callback = self.selected
        self.add_item(select)

    async def selected(self, i):
        await self.callback_fn(i, int(i.data['values'][0]))


class MainPanel(PrivateView):
    def __init__(self, bot, user_id):
        super().__init__(user_id)
        self.bot = bot

    @discord.ui.button(label='파티 관리', style=discord.ButtonStyle.primary)
    async def parties(self, i, button):
        bosses = await rows(self.bot,i,'bosses',state='all')
        bosses = [dict(b,label=f'{b["name"]} ({b["difficulty"]}) · {b["status"]}') for b in bosses if b['status']!='deleted']
        async def selected(j,bid):
            await asyncio.to_thread(self.bot.service.settings,actor(j))
            b = await asyncio.to_thread(self.bot.entity,actor(j),'bosses',bid)
            await j.response.send_message(f'{safe(b["name"])} ({safe(b["difficulty"])})', view=BossPanel(self.bot,j.user.id,bid),ephemeral=True)
        await pick(i,bosses,selected,'관리할 보스 선택')

    @discord.ui.button(label='캐릭터 관리', style=discord.ButtonStyle.primary)
    async def characters(self, i, button):
        await rows(self.bot,i,'characters',state='all')
        async def owner(j,uid):
            chars = await rows(self.bot,j,'characters',state='all',owner_id=uid)
            async def chosen(k,cid):
                await asyncio.to_thread(self.bot.service.settings,actor(k))
                ch = await asyncio.to_thread(self.bot.entity,actor(k),'characters',cid)
                await k.response.send_message(f'{safe(ch["name"])} · 헥환 {show_hexa(ch["hexa_value"])}',view=CharacterPanel(self.bot,k.user.id,cid),ephemeral=True)
            await pick(j,chars,chosen,'관리할 캐릭터 선택')
        await i.response.send_message('캐릭터 소유자를 선택해 주세요.',view=UserPicker(i.user.id,owner),ephemeral=True)

    @discord.ui.button(label='캐릭터 등록', style=discord.ButtonStyle.success)
    async def register(self, i, button):
        await rows(self.bot,i,'characters',state='all')
        async def owner(j,uid):
            jobs = await rows(self.bot,j,'jobs')
            async def job_selected(k,jid):
                async def submit(l,values):
                    payload={'owner_id':uid,'job_id':jid,'name':values[0]}
                    if values[1]: payload['hexa']=values[1]
                    await self.bot.mutate(l,'character.add',payload)
                await k.response.send_modal(TextForm('캐릭터 등록',[('캐릭터명',True,None),('헥환 (만 단위, 예: 5.25)',False,None)],submit))
            await pick(j,jobs,job_selected,'직업을 선택해 주세요.')
        await i.response.send_message('등록할 캐릭터의 소유자를 선택해 주세요.',view=UserPicker(i.user.id,owner),ephemeral=True)

    @discord.ui.button(label='조회', style=discord.ButtonStyle.secondary)
    async def query(self, i, button):
        chars=await rows(self.bot,i,'characters',state='all')
        await pick(i,chars,self.bot.char_query,'조회할 캐릭터 선택')


class BossPanel(PrivateView):
    def __init__(self,bot,uid,bid):
        super().__init__(uid)
        self.bot,self.bid=bot,bid

    @discord.ui.button(label='파티 선택',style=discord.ButtonStyle.primary)
    async def parties(self,i,button):
        groups=await rows(self.bot,i,'boss_groups',state='all',boss_id=self.bid)
        async def selected(j,gid):
            await asyncio.to_thread(self.bot.service.settings,actor(j))
            group=await asyncio.to_thread(self.bot.entity,actor(j),'boss_groups',gid)
            await j.response.send_message(safe(group['name']),view=PartyPanel(self.bot,j.user.id,gid),ephemeral=True)
        await pick(i,groups,selected)

    @discord.ui.button(label='빈 파티 생성',style=discord.ButtonStyle.success)
    async def create(self,i,button):
        async def submit(j,values):
            await self.bot.mutate(j,'group.add',{'boss_id':self.bid,'name':values[0] or None})
        await i.response.send_modal(TextForm('빈 파티 생성',[('파티명 (생략 가능)',False,None)],submit))

    @discord.ui.button(label='보스 현황',style=discord.ButtonStyle.secondary)
    async def show(self,i,button):
        await self.bot.boss_query(i,self.bid)


class PartyPanel(PrivateView):
    def __init__(self,bot,uid,gid):
        super().__init__(uid)
        self.bot,self.gid=bot,gid

    @discord.ui.button(label='캐릭터 추가',style=discord.ButtonStyle.success)
    async def add(self,i,button):
        chars=await rows(self.bot,i,'characters',state='active')
        async def selected(j,cid):
            await self.bot.mutate(j,'group.member',{'id':self.gid,'character_id':cid})
        await pick(i,chars,selected)

    async def members(self,i):
        chars=await rows(self.bot,i,'characters',state='all')
        with self.bot.service.db.transaction(False) as c:
            ids={r[0] for r in c.execute('SELECT character_id FROM group_members WHERE guild_id=? AND group_id=?',(actor(i).guild_id,self.gid))}
        return [r for r in chars if r['id'] in ids]

    @discord.ui.button(label='캐릭터 제외',style=discord.ButtonStyle.secondary)
    async def remove(self,i,button):
        async def selected(j,cid):
            await self.bot.mutate(j,'group.remove',{'id':self.gid,'character_id':cid})
        await pick(i,await self.members(i),selected)

    @discord.ui.button(label='캐릭터 이동',style=discord.ButtonStyle.primary)
    async def move(self,i,button):
        async def character(j,cid):
            group=await asyncio.to_thread(self.bot.entity,actor(j),'boss_groups',self.gid)
            targets=await rows(self.bot,j,'boss_groups',boss_id=group['boss_id'])
            targets=[r for r in targets if r['id']!=self.gid]
            async def target(k,tid):
                await self.bot.mutate(k,'group.move',{'id':self.gid,'target_id':tid,'character_id':cid})
            await pick(j,targets,target,'이동 대상 파티 선택')
        await pick(i,await self.members(i),character,'이동할 캐릭터 선택')

    @discord.ui.button(label='파티 병합',style=discord.ButtonStyle.secondary)
    async def merge(self,i,button):
        group=await asyncio.to_thread(self.bot.entity,actor(i),'boss_groups',self.gid)
        targets=await rows(self.bot,i,'boss_groups',boss_id=group['boss_id'])
        async def selected(j,tid):
            await self.bot.mutate(j,'group.merge',{'id':self.gid,'target_id':tid})
        await pick(i,[r for r in targets if r['id']!=self.gid],selected,'병합 대상 파티 선택 (현재 파티는 삭제)')

    @discord.ui.button(label='이름 변경',style=discord.ButtonStyle.secondary)
    async def rename(self,i,button):
        async def submit(j,values):
            await self.bot.mutate(j,'group.rename',{'id':self.gid,'name':values[0]})
        await i.response.send_modal(TextForm('파티 이름 변경',[('새 이름',True,None)],submit))

    @discord.ui.button(label='파티 삭제',style=discord.ButtonStyle.danger)
    async def delete(self,i,button):
        await self.bot.mutate(i,'group.delete',{'id':self.gid})


class CharacterPanel(PrivateView):
    def __init__(self,bot,uid,cid):
        super().__init__(uid)
        self.bot,self.cid=bot,cid

    @discord.ui.button(label='이름·헥환 수정',style=discord.ButtonStyle.primary)
    async def edit(self,i,button):
        async def submit(j,values):
            p={'id':self.cid}
            if values[0]:p['name']=values[0]
            if values[1]:p['hexa']=values[1]
            await self.bot.mutate(j,'character.edit',p)
        await i.response.send_modal(TextForm('캐릭터 수정 (공란은 유지)',[('이름',False,None),('헥환 (만 단위)',False,None)],submit))

    @discord.ui.button(label='직업 수정',style=discord.ButtonStyle.secondary)
    async def job(self,i,button):
        async def selected(j,jid):
            await self.bot.mutate(j,'character.edit',{'id':self.cid,'job_id':jid})
        await pick(i,await rows(self.bot,i,'jobs'),selected,'새 직업 선택')

    @discord.ui.button(label='소유자 정정',style=discord.ButtonStyle.secondary)
    async def owner(self,i,button):
        await rows(self.bot,i,'characters',state='all')
        async def selected(j,uid):
            await self.bot.mutate(j,'character.owner',{'id':self.cid,'owner_id':uid})
        await i.response.send_message('새 소유자를 선택해 주세요.',view=UserPicker(i.user.id,selected),ephemeral=True)

    @discord.ui.button(label='헥환 초기화',style=discord.ButtonStyle.secondary)
    async def reset(self,i,button):
        await self.bot.mutate(i,'character.edit',{'id':self.cid,'reset_hexa':True})

    @discord.ui.button(label='활성·비활성 전환',style=discord.ButtonStyle.secondary)
    async def active(self,i,button):
        ch=await asyncio.to_thread(self.bot.entity,actor(i),'characters',self.cid)
        await self.bot.mutate(i,'character.deactivate' if ch['active'] else 'character.activate',{'id':self.cid})

    @discord.ui.button(label='배정 조회',style=discord.ButtonStyle.secondary)
    async def show(self,i,button):
        await self.bot.char_query(i,self.cid)

    @discord.ui.button(label='파티 배정·이동',style=discord.ButtonStyle.success)
    async def assign(self,i,button):
        bosses=await rows(self.bot,i,'bosses')
        async def boss(j,bid):
            targets=await rows(self.bot,j,'boss_groups',boss_id=bid)
            with self.bot.service.db.transaction(False) as c:
                row=c.execute('SELECT group_id FROM group_members WHERE guild_id=? AND boss_id=? AND character_id=?',(actor(j).guild_id,bid,self.cid)).fetchone()
            origin=row[0] if row else None
            async def selected(k,tid):
                action='group.move' if origin else 'group.member'
                p={'id':origin or tid,'character_id':self.cid}
                if origin:p['target_id']=tid
                await self.bot.mutate(k,action,p)
            current=next((r['name'] for r in targets if r['id']==origin),'미배정')
            await pick(j,targets,selected,'현재 파티: '+safe(current)+' · 대상 선택')
        await pick(i,bosses,boss,'배정·이동할 보스 선택')
