import argparse
import asyncio
import logging
import os
from pathlib import Path
from .db import Database
from . import settings


def main():
    parser = argparse.ArgumentParser(description='메이플 보스 파티 배분 봇')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--token-set', action='store_true', help='Windows 자격 증명 관리자에 토큰 등록·교체')
    actions.add_argument('--token-delete', action='store_true', help='Windows에 저장한 토큰 삭제')
    actions.add_argument('--test-guild-set', metavar='SERVER_ID', help='테스트 서버 ID를 로컬 설정에 저장')
    actions.add_argument('--test-guild-delete', action='store_true', help='저장한 테스트 서버 ID 삭제 (환경 변수도 없으면 전역 동기화)')
    actions.add_argument('--check', action='store_true', help='토큰 없이 DB·명령어 구성 검증')
    actions.add_argument('--backup', metavar='DIRECTORY', help='SQLite 일관된 백업 생성')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        if args.token_set:
            settings.set_token()
            print('봇 토큰을 Windows 자격 증명 관리자에 저장했습니다. 기존 값은 교체됩니다.')
        elif args.token_delete:
            settings.delete_token()
            print('이 PC에 저장된 봇 토큰을 삭제했습니다. Discord 토큰 자체는 무효화되지 않습니다.')
        elif args.test_guild_set is not None:
            settings.set_test_guild(args.test_guild_set)
            print('테스트 서버 ID를 로컬 설정에 저장했습니다.')
        elif args.test_guild_delete:
            settings.delete_test_guild()
            print('저장된 테스트 서버 ID를 삭제했습니다. 환경 변수도 없으면 다음 실행에서 전역 동기화합니다.')
        else:
            return run_bot(parser, args)
        if (args.token_set or args.token_delete) and os.getenv('DISCORD_TOKEN'):
            print('주의: 현재 DISCORD_TOKEN 환경 변수가 저장된 토큰보다 우선합니다.')
        if (args.test_guild_set is not None or args.test_guild_delete) and 'DISCORD_TEST_GUILD_ID' in os.environ:
            print('주의: 현재 DISCORD_TEST_GUILD_ID 환경 변수가 로컬 설정보다 우선합니다.')
    except settings.SettingsError as error:
        parser.error(str(error))


def run_bot(parser, args):
    # Validate configuration before opening the DB or contacting Discord.
    target = settings.test_guild() if not args.backup else None
    token = settings.load_token() if not (args.check or args.backup) else None
    db = Database(os.getenv('MAPLEBOT_DB', 'var/maplebot.sqlite3'))
    if args.backup:
        print(db.backup(args.backup))
        return
    from .discord_app import MapleBot, MapleCommands
    if args.check:
        async def check():
            bot = MapleBot(db, test_guild_id=target)
            await bot.add_cog(MapleCommands(bot))
            commands = [c.to_dict(bot.tree) for c in bot.tree.get_commands()]
            print(f'DB 준비 완료 · 명령 그룹/단일 명령 {len(commands)}개 로드 완료')
            await bot.close()
        asyncio.run(check())
        return
    Path(os.getenv('MAPLEBOT_BACKUPS','var/backups')).mkdir(parents=True, exist_ok=True)
    db.backup(os.getenv('MAPLEBOT_BACKUPS','var/backups'))
    from discord import LoginFailure
    try:
        MapleBot(db, test_guild_id=target).run(token, log_handler=None)
    except LoginFailure:
        parser.error('Discord에서 봇 토큰을 거부했습니다. 환경 변수 우선순위를 확인하거나 토큰을 재발급·재등록하세요.')


if __name__ == '__main__':
    main()
