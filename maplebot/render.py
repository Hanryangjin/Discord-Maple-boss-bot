"""Pure renderers; pages stay below Discord embed limits."""
from .validation import show_hexa, UserError
import discord


def safe(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))


def pages(title, lines):
    title = safe(title)[:240]
    parts, buffer = [], ''
    for line in lines or ['등록된 항목이 없습니다.']:
        # Every line is bounded even if a display name contains unexpected input.
        line = str(line)[:1800]
        if len(buffer) + len(line) + 1 > 3500:
            parts.append(buffer)
            buffer = ''
        buffer += line + '\n'
    if buffer:
        parts.append(buffer)
    return [{'title': title, 'description': body} for body in parts]


def character_line(ch, job, owner):
    job_text = safe(job['name']) + (' (비활성 직업)' if not job['active'] else '')
    state = owner['membership'] if owner else 'unknown'
    if state == 'present':
        who = f'<@{ch["owner_id"]}>'
    else:
        who = safe(owner['display_name'] if owner else '사용자') + (' (서버 탈퇴)' if state == 'left' else ' (소속 확인 불가)')
    return f'**{safe(ch["name"])}** · {job_text} · 헥환 {show_hexa(ch["hexa_value"])} · {who}'


def boss_pages(c, guild, boss_id, *, public=True):
    boss = c.execute('SELECT * FROM bosses WHERE guild_id=? AND id=?', (guild, boss_id)).fetchone()
    if not boss:
        raise UserError('보스를 찾을 수 없습니다.')
    title = f'{boss["display_name"] or boss["name"]} ({boss["difficulty"]})'
    if public and boss['status'] != 'active':
        return pages(title, ['운영 중단' if boss['status'] == 'inactive' else '삭제된 보스'])
    if boss['status'] == 'deleted':
        return pages(title, ['삭제된 보스입니다. 서버 관리자는 /보스 복구를 사용할 수 있습니다.'])
    if public:
        from .cards import boss_data, party_pages, member_text
        data = boss_data(c, guild, boss_id)
        batches = party_pages(data['groups'])
        rendered = []
        for index, batch in enumerate(batches):
            lines = []
            for group in batch:
                lines.append(f'{group["name"]} ({len(group["members"])}/{boss["max_members"]})')
                lines.extend(member_text(member) for member in group['members'])
            rendered.append({'title': title, 'description': '\n'.join(lines) or '파티 0개',
                             'cards': batch, 'max_members': boss['max_members'],
                             'page': index, 'page_count': len(batches), 'group_count': len(data['groups'])})
        return rendered
    groups = c.execute("SELECT * FROM boss_groups WHERE guild_id=? AND boss_id=? AND status='active' ORDER BY sort_order,id", (guild, boss_id)).fetchall()
    lines = []
    for group in groups:
        chars = c.execute('''SELECT ch.* FROM group_members m JOIN characters ch ON ch.id=m.character_id
                            WHERE m.guild_id=? AND m.group_id=? ORDER BY m.joined_at,ch.id''', (guild, group['id'])).fetchall()
        lines.append(f'\n**{safe(group["name"])}** ({len(chars)}/{boss["max_members"]})')
        for ch in chars:
            job = c.execute('SELECT * FROM jobs WHERE guild_id=? AND id=?', (guild, ch['job_id'])).fetchone()
            owner = c.execute('SELECT * FROM discord_users WHERE guild_id=? AND user_id=?', (guild, ch['owner_id'])).fetchone()
            lines.append('• ' + character_line(ch, job, owner))
        if not chars:
            lines.append('빈 파티')
    return pages(title, lines or ['파티 0개'])


def character_pages(c, guild, char_id):
    ch = c.execute('SELECT * FROM characters WHERE guild_id=? AND id=?', (guild, char_id)).fetchone()
    if not ch:
        raise UserError('캐릭터를 찾을 수 없습니다.')
    job = c.execute('SELECT * FROM jobs WHERE guild_id=? AND id=?', (guild, ch['job_id'])).fetchone()
    owner = c.execute('SELECT * FROM discord_users WHERE guild_id=? AND user_id=?', (guild, ch['owner_id'])).fetchone()
    lines = [character_line(ch, job, owner), '활성 캐릭터' if ch['active'] else '비활성 캐릭터']
    bosses = c.execute("SELECT * FROM bosses WHERE guild_id=? AND status='active' ORDER BY sort_order,id", (guild,)).fetchall()
    for boss in bosses:
        group = c.execute('''SELECT gr.* FROM group_members m JOIN boss_groups gr ON gr.id=m.group_id
                    WHERE m.guild_id=? AND m.character_id=? AND m.boss_id=? AND gr.status='active' ''', (guild, char_id, boss['id'])).fetchone()
        prefix = f'\n**{safe(boss["display_name"] or boss["name"])} ({safe(boss["difficulty"])})**'
        if not group:
            lines.append(prefix + ': 배정 없음')
            continue
        others = c.execute('''SELECT ch.* FROM group_members m JOIN characters ch ON ch.id=m.character_id
                             WHERE m.group_id=? AND ch.id<>? ORDER BY ch.name''', (group['id'], char_id)).fetchall()
        lines.append(prefix + ' / ' + safe(group['name']) + f' ({len(others)+1}/{boss["max_members"]})')
        for other in others:
            who = c.execute('SELECT * FROM discord_users WHERE guild_id=? AND user_id=?', (guild, other['owner_id'])).fetchone()
            job = c.execute('SELECT * FROM jobs WHERE guild_id=? AND id=?', (guild, other['job_id'])).fetchone()
            lines.append('• ' + character_line(other, job, who))
        if not others:
            lines.append('동료 없음 (1인 파티)')
    return pages(ch['name']+' · 캐릭터 조회', lines)
