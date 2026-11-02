import argparse
import asyncio
import logging
import os
from pathlib import Path
from .db import Database


def main():
    parser = argparse.ArgumentParser(description='메이플 보스 파티 배분 봇')
    parser.add_argument('--check', action='store_true', help='토큰 없이 DB·명령어 구성 검증')
    parser.add_argument('--backup', metavar='DIRECTORY', help='SQLite 일관된 백업 생성')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    db = Database(os.getenv('MAPLEBOT_DB', 'var/maplebot.sqlite3'))
    if args.backup:
        print(db.backup(args.backup))
        return
    from .discord_app import MapleBot, MapleCommands
    if args.check:
        async def check():
            bot = MapleBot(db)
            await bot.add_cog(MapleCommands(bot))
            commands = [c.to_dict(bot.tree) for c in bot.tree.get_commands()]
            print(f'DB 준비 완료 · 명령 그룹/단일 명령 {len(commands)}개 로드 완료')
            await bot.close()
        asyncio.run(check())
        return
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        parser.error('DISCORD_TOKEN 환경 변수가 필요합니다. 토큰을 파일·로그에 기록하지 마세요.')
    Path(os.getenv('MAPLEBOT_BACKUPS','var/backups')).mkdir(parents=True, exist_ok=True)
    db.backup(os.getenv('MAPLEBOT_BACKUPS','var/backups'))
    MapleBot(db).run(token, log_handler=None)


if __name__ == '__main__':
    main()
