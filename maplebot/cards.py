"""Shared Components V2 cards; no network I/O while reading a snapshot."""
import discord
from .render import safe
from .validation import UserError, show_hexa


def boss_data(c, guild_id, boss_id):
    boss = c.execute('SELECT * FROM bosses WHERE guild_id=? AND id=?', (guild_id, boss_id)).fetchone()
    if not boss:
        raise UserError('보스를 찾을 수 없습니다.')
    result = dict(boss)
    result['groups'] = []
    for group in c.execute("SELECT * FROM boss_groups WHERE guild_id=? AND boss_id=? AND status='active' ORDER BY sort_order,id", (guild_id, boss_id)):
        item = dict(group)
        item['members'] = [dict(row) for row in c.execute('''SELECT ch.id,ch.name,ch.owner_id,ch.hexa_value,ch.job_id,j.icon_key AS job_icon_key,
            j.name AS job_name,j.active AS job_active,u.display_name,u.membership
            FROM group_members m JOIN characters ch ON ch.id=m.character_id
            JOIN jobs j ON j.id=ch.job_id LEFT JOIN discord_users u
            ON u.guild_id=ch.guild_id AND u.user_id=ch.owner_id
            WHERE m.guild_id=? AND m.group_id=? ORDER BY m.joined_at,ch.id''', (guild_id, group['id']))]
        result['groups'].append(item)
    return result


def member_text(member):
    job = safe(member['job_name']) + (' · 비활성 직업' if not member['job_active'] else '')
    if member.get('membership') == 'present':
        owner = f'<@{member["owner_id"]}>'
    else:
        owner = safe(member.get('display_name') or '사용자')
        owner += ' · 서버 탈퇴' if member.get('membership') == 'left' else ' · 소속 확인 불가'
    return f'### {safe(member["name"])}\n{job} · **헥환 {show_hexa(member["hexa_value"])}**\n{owner}'


def avatar_url(bot, guild_id, owner_id):
    guild = bot.get_guild(guild_id)
    user = (guild.get_member(owner_id) if guild else None) or bot.get_user(owner_id)
    if user:
        return str(user.display_avatar.replace(size=128, static_format='png'))
    return f'https://cdn.discordapp.com/embed/avatars/{(owner_id >> 22) % 6}.png'


def party_pages(groups, *, component_budget=24, text_budget=2800):
    """Private panels reserve navigation space; public boards use a larger budget."""
    result, batch, count, length = [], [], 0, 0
    for group in groups:
        cost = 6 + len(group['members']) * 3 if group['members'] else 7
        text_cost = len(group['name']) * 2 + sum(len(member_text(m)) for m in group['members']) + 100
        if batch and (count + cost > component_budget or length + text_cost > text_budget):
            result.append(batch)
            batch, count, length = [], 0, 0
        batch.append(group)
        count += cost
        length += text_cost
    if batch or not result:
        result.append(batch)
    return result


def party_card(bot, guild_id, group, maximum, controls=None, thumbnails=None):
    card = discord.ui.Container(accent_colour=0x65AD86)
    card.add_item(discord.ui.TextDisplay(f'## {safe(group["name"])}　{len(group["members"])}/{maximum}명'))
    card.add_item(discord.ui.Separator())
    for member in group['members']:
        card.add_item(discord.ui.Section(
            discord.ui.TextDisplay(member_text(member)),
            accessory=discord.ui.Thumbnail((thumbnails or {}).get(member['id']) or avatar_url(bot, guild_id, member['owner_id']),
                description='직업 SD 얼굴 아이콘과 Discord 아바타' if (thumbnails or {}).get(member['id']) else '소유자 Discord 아바타'),
        ))
    if not group['members']:
        card.add_item(discord.ui.TextDisplay('아직 구성원이 없습니다. 편집에서 캐릭터를 추가하세요.'))
    if controls:
        card.add_item(discord.ui.ActionRow(*controls))
    return card


def validate_layout(view):
    if view.total_children_count > 40 or view.content_length() > 4000:
        raise ValueError('Components V2 message exceeds Discord limits')
    return view
