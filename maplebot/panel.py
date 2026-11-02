"""One ephemeral message per management session, using Components V2."""
import asyncio
import logging
import sqlite3
import discord
from .cards import boss_data, party_card, party_pages, validate_layout, member_text, avatar_url
from .render import safe, character_pages
from .validation import UserError, key, show_hexa

log = logging.getLogger(__name__)


def route(kind='home', **values):
    return {'kind': kind, 'page': 0, **values}


class PanelLayout(discord.ui.LayoutView):
    def __init__(self, session):
        super().__init__(timeout=300)
        self.session = session
        self.revision = session.revision

    async def interaction_check(self, i):
        if i.user.id != self.session.user_id or i.guild_id != self.session.guild_id:
            await i.response.send_message('이 화면을 연 사용자만 사용할 수 있습니다.', ephemeral=True)
            return False
        return True

    async def on_error(self, i, error, item):
        await self.session.error(i, error)

    async def on_timeout(self):
        async with self.session.lock:
            if self.session.view is not self or not self.session.active:
                return
            self.session.active = False
            expired = discord.ui.LayoutView(timeout=None)
            expired.add_item(discord.ui.TextDisplay('## 관리 화면이 만료되었습니다\n공개 배분표의 관리 패널 또는 `/관리 패널`로 다시 열어 주세요.'))
            if self.session.message:
                try:
                    await self.session.message.edit(content=None, embeds=[], view=expired, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
            # Retain the inactive root so reopening can remove the expired panel.


class TextForm(discord.ui.Modal):
    def __init__(self, session, title, fields, callback):
        super().__init__(title=title, timeout=300)
        self.session, self.callback_fn, self.revision = session, callback, session.revision
        self.inputs = []
        for name, required, default in fields:
            item = discord.ui.TextInput(label=name, required=required, default=default, max_length=100)
            self.inputs.append(item)
            self.add_item(item)

    async def on_submit(self, i):
        async def submit():
            await self.callback_fn(i, [str(item.value) for item in self.inputs])
        await self.session.handle(i, submit, expected=self.revision)

    async def on_error(self, i, error):
        await self.session.error(i, error)


class PanelSession:
    def __init__(self, bot, user_id, guild_id, initial=None):
        self.bot, self.user_id, self.guild_id = bot, user_id, guild_id
        self.current = initial or route()
        self.history = []
        self.lock = asyncio.Lock()
        self.revision = 0
        self.view = None
        self.message = None
        self.active = True
        self.notice = ''

    def unregister(self):
        sessions = getattr(self.bot, 'panel_sessions', {})
        if sessions.get((self.guild_id, self.user_id)) is self:
            sessions.pop((self.guild_id, self.user_id), None)

    async def start(self, i):
        # The caller creates exactly one ephemeral root, never edits the public board.
        if not i.response.is_done():
            await i.response.defer(ephemeral=True, thinking=True)
        await self.check(i)
        await self.render(i)

    async def check(self, i):
        from .discord_app import actor
        if i.user.id != self.user_id or i.guild_id != self.guild_id:
            raise UserError('현재 관리 세션의 사용자·서버가 일치하지 않습니다.')
        await asyncio.to_thread(self.bot.service.settings, actor(i))

    async def handle(self, i, callback, *, expected=None, modal=False):
        # Modal must be the immediate interaction response. Other work is deferred as
        # a message update (type 6), not a new ephemeral followup (type 5).
        if not modal and not i.response.is_done():
            await i.response.defer(thinking=False)
        async with self.lock:
            checked=False
            try:
                if not self.active:
                    raise UserError('만료된 관리 화면입니다. /관리 패널로 다시 열어 주세요.')
                await self.check(i)
                checked=True
                if expected is not None and expected != self.revision:
                    raise UserError('이전 화면의 요청입니다. 현재 화면에서 다시 선택해 주세요.')
                self.notice = ''
                await callback()
            except Exception as error:
                if not checked:
                    self.active=False
                    self.unregister()
                    if self.view:self.view.stop()
                await self.error(i, error)

    async def error(self, i, error):
        if not isinstance(error, UserError):
            log.exception('management UI failed', exc_info=error)
        self.notice = str(error) if isinstance(error, UserError) else '처리하지 못했습니다. 잠시 후 다시 시도해 주세요.'
        if not i.response.is_done():
            await i.response.defer(thinking=False)
        if not self.active:
            closed = discord.ui.LayoutView(timeout=None)
            closed.add_item(discord.ui.TextDisplay('## 관리 화면 종료\n'+safe(self.notice)))
            await i.edit_original_response(content=None, embeds=[], view=closed, allowed_mentions=discord.AllowedMentions.none())
            return
        await self.render(i)

    async def go(self, i, target, *, push=True):
        if push:
            self.history.append(self.current)
        self.current = target
        await self.render(i)

    async def back(self, i):
        self.current = self.history.pop() if self.history else route()
        await self.render(i)

    async def home(self, i):
        self.history.clear()
        self.current = route()
        await self.render(i)

    async def close(self, i):
        self.active = False
        if self.view:
            self.view.stop()
        self.unregister()
        await i.delete_original_response()

    async def render(self, i):
        self.revision += 1
        snapshot = await asyncio.to_thread(self.read_snapshot)
        try:
            try:
                view = self.build_view(snapshot)
            except UserError as error:
                self.notice = str(error)
                self.current = route('boss', boss_id=self.current['boss_id']) if self.current.get('boss_id') and self.current['kind'] != 'boss' else route()
                view = self.build_view(snapshot)
        finally:
            snapshot.close()
        validate_layout(view)
        if self.view:
            self.view.stop()
        self.view = view
        await i.edit_original_response(content=None, embeds=[], view=view, allowed_mentions=discord.AllowedMentions.none())
        self.message = await i.original_response()

    async def form(self, i, title, fields, callback):
        await i.response.send_modal(TextForm(self, title, fields, callback))

    async def mutate(self, i, action, payload, return_to, token=None):
        result = await self.bot.perform_mutation(i, action, payload, token)
        if result['status'] == 'confirm':
            await self.go(i, route('confirm', action=action, payload=payload, token=result['token'],
                                   summary=result['message'], return_to=return_to), push=False)
        else:
            self.notice = '✓ ' + result['message']
            self.current = return_to
            await self.render(i)

    def button(self, text, callback, *, style=discord.ButtonStyle.secondary, disabled=False, modal=False):
        button = discord.ui.Button(label=text, style=style, disabled=disabled)
        expected = self.revision
        async def run(i):
            async def invoke():
                await callback(i)
            await self.handle(i, invoke, expected=expected, modal=modal)
        button.callback = run
        return button

    def dropdown(self, rows, callback, placeholder, *, page=0):
        select = discord.ui.Select(placeholder=placeholder[:150], options=[
            discord.SelectOption(label=r.get('label', r['name'])[:100], value=str(r['id'])) for r in rows[page*25:page*25+25]
        ])
        expected = self.revision
        async def selected(i):
            await self.handle(i, lambda: callback(i, int(i.data['values'][0])), expected=expected)
        select.callback = selected
        return discord.ui.ActionRow(select)

    def navigation(self):
        if self.current['kind']=='boss' and getattr(self,'page_navigation',None):
            total,page=self.page_navigation
            r=self.current
            return discord.ui.ActionRow(
                self.button('뒤로',self.back,disabled=not self.history),
                self.button('관리 홈',self.home),
                self.button('이전 파티',lambda i:self.go(i,{**r,'page':page-1},push=False),disabled=page==0),
                self.button('다음 파티',lambda i:self.go(i,{**r,'page':page+1},push=False),disabled=page>=total-1),
                self.button('닫기',self.close),
            )
        return discord.ui.ActionRow(
            self.button('뒤로', self.back, disabled=not self.history),
            self.button('관리 홈', self.home),
            self.button('새로고침', lambda i: self.render(i)),
            self.button('닫기', self.close),
        )

    def pager(self, total, size=1):
        r = self.current
        page = r.get('page', 0)
        last = max(0, (total-1)//size)
        async def move(i, delta):
            await self.go(i, {**r, 'page': max(0, min(last, page+delta))}, push=False)
        return discord.ui.ActionRow(
            self.button('이전 페이지', lambda i: move(i,-1), disabled=page==0),
            self.button(f'{page+1}/{last+1}', lambda i: self.render(i), disabled=True),
            self.button('다음 페이지', lambda i: move(i,1), disabled=page>=last),
        )

    def load(self, c, table, entity_id):
        return self.bot.service.get(c, self.guild_id, table, entity_id)

    def picker(self, title, items, callback, return_to, *, page=0, query=''):
        return route('picker', title=title, items=items, callback=callback, return_to=return_to, page=page, query=query)

    async def choose_owner(self, i, callback, title='캐릭터 소유자 선택'):
        await self.go(i, route('owner', title=title, callback=callback))

    def rows(self, c, table, **filters):
        sql = f'SELECT * FROM {table} WHERE guild_id=?'
        args = [self.guild_id]
        for k,v in filters.items():
            sql += f' AND {k}=?'
            args.append(v)
        return [dict(r) for r in c.execute(sql+' ORDER BY id',args)]

    def read_snapshot(self):
        # Read SQLite on a worker; construct Discord views on the running event loop.
        tables=('guild_settings','discord_users','jobs','cycles','characters','bosses','boss_groups','group_members')
        memory=sqlite3.connect(':memory:',check_same_thread=False)
        memory.row_factory=sqlite3.Row
        try:
            with self.bot.service.db.transaction(False) as c:
                for table in tables:
                    schema=c.execute("SELECT sql FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone()[0]
                    memory.execute(schema)
                    rows=c.execute(f'SELECT * FROM {table} WHERE guild_id=?',(self.guild_id,)).fetchall()
                    if rows:
                        memory.executemany(f'INSERT INTO {table} VALUES('+','.join('?' for _ in rows[0])+')',[tuple(row) for row in rows])
            return memory
        except BaseException:
            memory.close()
            raise

    def build(self):
        snapshot=self.read_snapshot()
        try:return self.build_view(snapshot)
        finally:snapshot.close()

    def build_view(self, c):
        r = self.current
        view = PanelLayout(self)
        self.page_navigation=None
        titles = {'home':'관리 홈','boss':'파티 관리','party':'파티 편집','owner':'소유자 선택',
                  'characters':'캐릭터 관리','character':'캐릭터 상세','picker':'선택','confirm':'변경 확인','query':'조회'}
        heading='# 메이플 보스 관리\n-# 관리 홈 › '+titles.get(r['kind'],'관리')
        if self.notice:heading+='\n'+safe(self.notice)[:700]
        view.add_item(discord.ui.TextDisplay(heading))
        view.add_item(discord.ui.ActionRow(
            self.button('파티 관리', lambda i: self.go(i,route('boss')),style=discord.ButtonStyle.primary if r['kind'] in ('boss','party') else discord.ButtonStyle.secondary),
            self.button('캐릭터 관리', lambda i: self.choose_owner(i,self.show_characters)),
            self.button('조회', lambda i: self.show_query_picker(i)),
        ))
        kind = r['kind']
        if kind == 'home':
            view.add_item(discord.ui.Container(discord.ui.TextDisplay('## 파티를 한눈에 관리하세요\n보스를 선택해 파티 카드에서 편집하거나, 캐릭터를 등록·수정하세요.\n모든 작업은 이 화면 안에서 진행됩니다.'),accent_colour=0x65AD86))
            view.add_item(discord.ui.ActionRow(self.button('＋ 캐릭터 등록',self.register,style=discord.ButtonStyle.success)))
        elif kind == 'boss':
            self.build_boss(c,view)
        elif kind == 'party':
            self.build_party(c,view)
        elif kind == 'owner':
            view.add_item(discord.ui.TextDisplay('## '+safe(r['title'])))
            select = discord.ui.UserSelect(placeholder='Discord 소유자를 선택하세요')
            expected=self.revision
            async def selected(i):
                await self.handle(i,lambda:r['callback'](i,int(i.data['values'][0])),expected=expected)
            select.callback=selected
            view.add_item(discord.ui.ActionRow(select))
        elif kind == 'characters':
            self.build_characters(c,view)
        elif kind == 'character':
            self.build_character(c,view)
        elif kind == 'picker':
            self.build_picker(view)
        elif kind == 'confirm':
            view.add_item(discord.ui.Container(discord.ui.TextDisplay('## 실행 전 확인\n'+safe(r['summary'])[:2100]),accent_colour=0xEDAD43))
            view.add_item(discord.ui.ActionRow(
                self.button('확인하고 실행',lambda i:self.mutate(i,r['action'],r['payload'],r['return_to'],r['token']),style=discord.ButtonStyle.danger),
                self.button('취소',lambda i:self.go(i,r['return_to'],push=False)),
                self.button('최신 정보로 다시 확인',lambda i:self.mutate(i,r['action'],r['payload'],r['return_to'])),
            ))
        elif kind == 'query':
            self.load(c,'characters',r['char_id'])
            raw=character_pages(c,self.guild_id,r['char_id'])
            content=[{**p,'description':p['description'][start:start+2800]} for p in raw for start in range(0,max(1,len(p['description'])),2800)]
            page=min(r['page'],len(content)-1)
            self.current={**r,'page':page}
            view.add_item(discord.ui.Container(discord.ui.TextDisplay('## '+content[page]['title']+'\n'+content[page]['description']),accent_colour=0x65AD86))
            view.add_item(self.pager(len(content)))
        view.add_item(self.navigation())
        return view

    def build_boss(self,c,view):
        r=self.current
        bosses=self.rows(c,'bosses')
        bosses=[dict(b,label=f'{b["display_name"] or b["name"]} ({b["difficulty"]})'+(' · 운영 중단' if b['status']=='inactive' else '')) for b in bosses if b['status']!='deleted']
        if not bosses:
            view.add_item(discord.ui.TextDisplay('## 등록된 보스가 없습니다\n`/보스 추가`로 운영 보스를 먼저 등록해 주세요.'))
            return
        bid=r.get('boss_id')
        if bid is None:
            bid=bosses[0]['id']
            self.current=r={**r,'boss_id':bid}
        data=boss_data(c,self.guild_id,bid)
        if data['status']=='deleted':
            raise UserError('삭제된 보스입니다. 보스 복구 후 사용해 주세요.')
        async def selected(i,value):
            await self.go(i,route('boss',boss_id=value),push=False)
        if len(bosses)<=25:
            view.add_item(self.dropdown(bosses,selected,'보스 선택 · '+data['name']))
        else:
            view.add_item(discord.ui.ActionRow(self.button('보스 검색·선택',lambda i:self.go(i,self.picker('보스 선택',bosses,selected,r)))))
        batches=party_pages(data['groups'])
        page=min(r['page'],len(batches)-1)
        self.current={**r,'page':page}
        title=f'## {safe(data["display_name"] or data["name"])} ({safe(data["difficulty"])})\n{len(data["groups"])}개 파티 · 정원 {data["max_members"]}명 · {page+1}/{len(batches)}페이지'
        if data['status']=='inactive':title+=' · **운영 중단**'
        view.add_item(discord.ui.TextDisplay(title))
        view.add_item(discord.ui.ActionRow(self.button('＋ 파티 생성',lambda i:self.create_party(i,bid),style=discord.ButtonStyle.success,disabled=data['status']!='active',modal=True)))
        for group in batches[page]:
            gid=group['id']
            destination=route('party',boss_id=bid,group_id=gid)
            controls=[self.button('편집',lambda i,d=destination:self.go(i,d),style=discord.ButtonStyle.primary),
                      self.button('삭제',lambda i,g=gid:self.mutate(i,'group.delete',{'id':g},route('boss',boss_id=bid)),style=discord.ButtonStyle.danger,disabled=data['status']!='active')]
            view.add_item(party_card(self.bot,self.guild_id,group,data['max_members'],controls))
        if not data['groups']:
            view.add_item(discord.ui.TextDisplay('아직 파티가 없습니다. **＋ 파티 생성**으로 시작하세요.'))
        # Merge page navigation into bottom row to keep six-avatar parties under 40 components.
        self.add_compact_paging(view,len(batches),page)

    def add_compact_paging(self,view,total,page):
        if total>1:
            self.page_navigation=(total,page)

    async def create_party(self,i,bid):
        async def submit(j,values):
            await self.mutate(j,'group.add',{'boss_id':bid,'name':values[0] or None},route('boss',boss_id=bid))
        await self.form(i,'빈 파티 생성',[('파티명 (생략 가능)',False,None)],submit)

    def build_party(self,c,view):
        r=self.current
        group=self.load(c,'boss_groups',r['group_id'])
        data=boss_data(c,self.guild_id,group['boss_id'])
        card=next((g for g in data['groups'] if g['id']==group['id']),None)
        if not card:raise UserError('삭제된 파티입니다.')
        view.add_item(discord.ui.TextDisplay('## '+safe(data['name'])+' · 파티 편집'))
        view.add_item(party_card(self.bot,self.guild_id,card,data['max_members']))
        active=data['status']=='active'
        view.add_item(discord.ui.ActionRow(
            self.button('＋ 캐릭터 추가',lambda i:self.pick_member(i,'add'),disabled=not active),
            self.button('캐릭터 제외',lambda i:self.pick_member(i,'remove')),
            self.button('다른 파티로 이동',lambda i:self.pick_member(i,'move'),disabled=not active),
        ))
        view.add_item(discord.ui.ActionRow(
            self.button('이름 변경',self.rename_party,disabled=not active,modal=True),
            self.button('파티 병합',self.merge_party,disabled=not active),
            self.button('파티 삭제',lambda i:self.mutate(i,'group.delete',{'id':group['id']},route('boss',boss_id=group['boss_id'])),style=discord.ButtonStyle.danger,disabled=not active),
        ))

    async def rename_party(self,i):
        r=self.current.copy()
        async def submit(j,values):
            await self.mutate(j,'group.rename',{'id':r['group_id'],'name':values[0]},r)
        await self.form(i,'파티 이름 변경',[('새 이름',True,None)],submit)

    async def pick_member(self,i,operation):
        r=self.current.copy()
        with self.bot.service.db.transaction(False) as c:
            chars=self.rows(c,'characters')
            if operation=='add':chars=[ch for ch in chars if ch['active']]
            else:
                ids={row[0] for row in c.execute('SELECT character_id FROM group_members WHERE guild_id=? AND group_id=?',(self.guild_id,r['group_id']))}
                chars=[ch for ch in chars if ch['id'] in ids]
        async def chosen(j,cid):
            if operation=='move':
                await self.choose_target(j,r,lambda k,tid:self.mutate(k,'group.move',{'id':r['group_id'],'character_id':cid,'target_id':tid},r))
            else:
                await self.mutate(j,'group.member' if operation=='add' else 'group.remove',{'id':r['group_id'],'character_id':cid},r)
        await self.go(i,self.picker('캐릭터 선택',chars,chosen,r))

    async def choose_target(self,i,r,callback):
        with self.bot.service.db.transaction(False) as c:
            groups=self.rows(c,'boss_groups',boss_id=r['boss_id'],status='active')
        groups=[dict(g,label=f'{g["name"]} · #{g["id"]}') for g in groups if g['id']!=r.get('group_id')]
        await self.go(i,self.picker('대상 파티 선택',groups,callback,r))

    async def merge_party(self,i):
        r=self.current.copy()
        await self.choose_target(i,r,lambda j,tid:self.mutate(j,'group.merge',{'id':r['group_id'],'target_id':tid},route('boss',boss_id=r['boss_id'])))

    async def show_characters(self,i,owner_id):
        await self.go(i,route('characters',owner_id=owner_id))

    def build_characters(self,c,view):
        r=self.current
        chars=self.rows(c,'characters',owner_id=r['owner_id'])
        chars=[dict(ch,label=ch['name']+(' · 비활성' if not ch['active'] else '')) for ch in chars]
        view.add_item(discord.ui.TextDisplay(f'## 캐릭터 관리\n소유자: <@{r["owner_id"]}> · {len(chars)}개 캐릭터'))
        page=min(r['page'],max(0,(len(chars)-1)//25))
        self.current={**r,'page':page}
        async def chosen(i,cid):await self.go(i,route('character',char_id=cid,owner_id=r['owner_id']))
        if chars:view.add_item(self.dropdown(chars,chosen,'캐릭터 선택 (비활성 포함)',page=page))
        else:view.add_item(discord.ui.TextDisplay('등록된 캐릭터가 없습니다.'))
        view.add_item(discord.ui.ActionRow(self.button('＋ 이 사용자의 캐릭터 등록',lambda i:self.register(i,r['owner_id']),style=discord.ButtonStyle.success)))
        view.add_item(self.pager(len(chars),25))

    def build_character(self,c,view):
        r=self.current
        ch=self.load(c,'characters',r['char_id'])
        job=self.load(c,'jobs',ch['job_id'])
        owner=c.execute('SELECT * FROM discord_users WHERE guild_id=? AND user_id=?',(self.guild_id,ch['owner_id'])).fetchone()
        member={**ch,'job_name':job['name'],'job_active':job['active'],
                'display_name':owner['display_name'] if owner else '', 'membership':owner['membership'] if owner else 'unknown'}
        view.add_item(discord.ui.Container(discord.ui.Section(discord.ui.TextDisplay(member_text(member)),
            accessory=discord.ui.Thumbnail(avatar_url(self.bot,self.guild_id,ch['owner_id']))),
            discord.ui.TextDisplay('활성 캐릭터' if ch['active'] else '비활성 캐릭터'),accent_colour=0x65AD86))
        view.add_item(discord.ui.ActionRow(
            self.button('이름·헥환 수정',self.edit_character,modal=True),self.button('직업 수정',self.edit_job),
            self.button('소유자 정정',self.edit_owner),self.button('헥환 초기화',lambda i:self.mutate(i,'character.edit',{'id':ch['id'],'reset_hexa':True},r)),
        ))
        view.add_item(discord.ui.ActionRow(
            self.button('비활성화' if ch['active'] else '활성화',lambda i:self.mutate(i,'character.deactivate' if ch['active'] else 'character.activate',{'id':ch['id']},r)),
            self.button('배정 조회',lambda i:self.go(i,route('query',char_id=ch['id']))),
            self.button('파티 배정·이동',self.assign_character,style=discord.ButtonStyle.success,disabled=not ch['active']),
        ))

    async def edit_character(self,i):
        r=self.current.copy()
        async def submit(j,values):
            p={'id':r['char_id']}
            if values[0]:p['name']=values[0]
            if values[1]:p['hexa']=values[1]
            await self.mutate(j,'character.edit',p,r)
        await self.form(i,'캐릭터 수정 (공란은 유지)',[('캐릭터명',False,None),('헥환 (만 단위, 예: 5.25)',False,None)],submit)

    async def edit_job(self,i):
        r=self.current.copy()
        with self.bot.service.db.transaction(False) as c:jobs=self.rows(c,'jobs',active=1)
        await self.go(i,self.picker('새 직업 선택',jobs,lambda j,jid:self.mutate(j,'character.edit',{'id':r['char_id'],'job_id':jid},r),r))

    async def edit_owner(self,i):
        r=self.current.copy()
        await self.choose_owner(i,lambda j,uid:self.mutate(j,'character.owner',{'id':r['char_id'],'owner_id':uid},r),'새 소유자 선택')

    async def register(self,i,owner_id=None):
        if owner_id is None:
            await self.choose_owner(i,lambda j,uid:self.register(j,uid),'등록할 캐릭터 소유자 선택')
            return
        origin=self.current
        with self.bot.service.db.transaction(False) as c:jobs=self.rows(c,'jobs',active=1)
        async def chosen(j,jid):
            # A picker normally defers; use a separate modal button screen instead.
            await self.go(j,route('picker',title='선택한 직업으로 등록',items=[],callback=None,return_to=origin,
                                  registration=(owner_id,jid)))
        await self.go(i,self.picker('직업 선택',jobs,chosen,origin))

    async def registration_form(self,i,owner_id,job_id):
        async def submit(j,values):
            p={'name':values[0],'job_id':job_id,'owner_id':owner_id}
            if values[1]:p['hexa']=values[1]
            await self.mutate(j,'character.add',p,route('characters',owner_id=owner_id))
        await self.form(i,'캐릭터 등록',[('캐릭터명',True,None),('헥환 (만 단위, 예: 5.25)',False,None)],submit)

    async def assign_character(self,i):
        r=self.current.copy()
        with self.bot.service.db.transaction(False) as c:bosses=self.rows(c,'bosses',status='active')
        async def chosen(j,bid):
            with self.bot.service.db.transaction(False) as c:
                groups=self.rows(c,'boss_groups',boss_id=bid,status='active')
                original=c.execute('SELECT group_id FROM group_members WHERE guild_id=? AND boss_id=? AND character_id=?',(self.guild_id,bid,r['char_id'])).fetchone()
            origin=original[0] if original else None
            current=next((g['name'] for g in groups if g['id']==origin),'미배정')
            async def target(k,tid):
                p={'id':origin or tid,'character_id':r['char_id']}
                if origin:p['target_id']=tid
                await self.mutate(k,'group.move' if origin else 'group.member',p,r)
            await self.go(j,self.picker('대상 파티 선택 · 현재 '+current,groups,target,r))
        await self.go(i,self.picker('배정할 보스 선택',bosses,chosen,r))

    async def show_query_picker(self,i):
        with self.bot.service.db.transaction(False) as c:chars=self.rows(c,'characters')
        await self.go(i,self.picker('캐릭터 조회',chars,lambda j,cid:self.go(j,route('query',char_id=cid)),self.current))

    def build_picker(self,view):
        r=self.current
        view.add_item(discord.ui.TextDisplay('## '+safe(r['title'])))
        if r.get('registration'):
            uid,jid=r['registration']
            view.add_item(discord.ui.TextDisplay(f'소유자: <@{uid}>\n아래 버튼에서 이름과 헥환을 입력하세요.'))
            view.add_item(discord.ui.ActionRow(self.button('등록 정보 입력',lambda i:self.registration_form(i,uid,jid),style=discord.ButtonStyle.success,modal=True)))
            return
        items=[item for item in r['items'] if key(r.get('query','')) in key(item.get('label',item['name']))]
        page=min(r['page'],max(0,(len(items)-1)//25))
        self.current={**r,'page':page}
        if items:view.add_item(self.dropdown(items,r['callback'],'항목 선택',page=page))
        else:view.add_item(discord.ui.TextDisplay('선택 가능한 항목이 없습니다. 뒤로 가거나 검색어를 바꿔 주세요.'))
        async def search(i):
            async def submit(j,values):await self.go(j,{**r,'query':values[0],'page':0},push=False)
            await self.form(i,'이름 검색',[('검색어 (공란은 전체)',False,r.get('query',''))],submit)
        view.add_item(discord.ui.ActionRow(self.button('이름 검색',search,modal=True)))
        view.add_item(self.pager(len(items),25))
