"""Card board layout and restart-safe buttons with non-visible recovery metadata."""
import re
import discord
from .cards import party_card, image_party_card, validate_layout
from .render import safe
from .validation import UserError

PATTERN = (r'maple:ui:(?P<guild>[0-9a-z]+):(?P<generation>[0-9a-z]+):'
           r'(?P<boss>[0-9a-z]+):(?P<page>[0-9a-z]+):(?P<verb>manage|mine|edit|delete):(?P<group>[0-9a-z]+)')


def base36(number):
    number = int(number)
    alphabet = '0123456789abcdefghijklmnopqrstuvwxyz'
    result = ''
    while number:
        number, digit = divmod(number,36)
        result = alphabet[digit]+result
    return result or '0'


def parse_marker(marker):
    pieces = marker.split(':')
    if len(pieces)==5 and pieces[0]=='maple' and all(p.isdecimal() for p in pieces[1:]):
        return tuple(int(p) for p in pieces[1:])
    return None


def message_marker(message):
    # Compatibility with already-posted Embed boards is essential during migration.
    for embed in message.embeds:
        if parse_marker(embed.footer.text or ''):
            return embed.footer.text
    def walk(components):
        for item in components:
            yield item
            yield from walk(getattr(item,'children',()))
            accessory=getattr(item,'accessory',None)
            if accessory:yield accessory
    for item in walk(message.components):
        match=re.fullmatch(PATTERN,getattr(item,'custom_id','') or '')
        if match:
            values=[int(match[k],36) for k in ('guild','generation','boss','page')]
            return 'maple:'+':'.join(str(v) for v in values)
    return None


class PublicAction(discord.ui.DynamicItem[discord.ui.Button], template=PATTERN):
    def __init__(self, guild, generation, boss, page, verb, group=0):
        self.guild_id,self.generation,self.boss_id,self.page,self.verb,self.group_id=guild,generation,boss,page,verb,group
        labels={'manage':'관리 패널','mine':'내 보스','edit':'편집','delete':'삭제'}
        identifier='maple:ui:'+':'.join(base36(v) for v in (guild,generation,boss,page))+':'+verb+':'+base36(group)
        super().__init__(discord.ui.Button(label=labels[verb],custom_id=identifier,
            style=discord.ButtonStyle.danger if verb=='delete' else discord.ButtonStyle.primary if verb in ('edit','mine') else discord.ButtonStyle.secondary))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(*(int(match[k],36) for k in ('guild','generation','boss','page')),match['verb'],int(match['group'],36))

    async def callback(self,i):
        from .discord_app import report
        try:
            if i.guild_id!=self.guild_id or not i.message or i.message.author.id!=i.client.user.id:
                raise UserError('유효하지 않은 파티 배분표입니다.')
            def current():
                with i.client.service.db.transaction(False) as c:
                    r=c.execute('SELECT active_generation FROM guild_settings WHERE guild_id=?',(self.guild_id,)).fetchone()
                    return r and r[0]==self.generation
            import asyncio
            if not await asyncio.to_thread(current):
                raise UserError('이전되었거나 준비·중단된 파티 배분표입니다. 현재 게시물을 사용해 주세요.')
            if self.verb=='mine':
                await i.client.mine(i)
            else:
                await i.client.open_panel(i,boss_id=self.boss_id,group_id=self.group_id or None,delete_group=self.verb=='delete')
        except Exception as error:
            await report(i,error)


def board_layout(bot, body, marker, thumbnails=None, party_images=None):
    view=discord.ui.LayoutView(timeout=None)
    identity=parse_marker(marker)
    continuation=body.get('page',0)>0
    title=('-# ' if continuation else '# ')+safe(body['title'])
    if continuation:
        title+=' · 계속'
    elif 'group_count' in body:
        title+=f'\n-# 전체 {body["group_count"]}개 파티'
    view.add_item(discord.ui.TextDisplay(title))
    if 'cards' in body:
        for group in body['cards']:
            controls=[PublicAction(*identity,'edit',group['id']),PublicAction(*identity,'delete',group['id'])] if identity else None
            if party_images and group['id'] in party_images:
                view.add_item(image_party_card(group,body['max_members'],party_images[group['id']],controls))
            else:
                view.add_item(party_card(bot,identity[0],group,body['max_members'],controls,thumbnails))
        if not body['cards']:
            view.add_item(discord.ui.Container(discord.ui.TextDisplay('## 파티 0개\n관리 패널에서 첫 파티를 생성하세요.'),accent_colour=0x65AD86))
    else:
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(body['description'][:3000]),accent_colour=0x65AD86))
    if identity and body.get('page',0)==body.get('page_count',1)-1:
        view.add_item(discord.ui.ActionRow(PublicAction(*identity,'manage'),PublicAction(*identity,'mine')))
    return validate_layout(view)
