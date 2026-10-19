"""
  python main.py            — зібрати й проаналізувати нові пости
  python main.py day        — + зведення українських джерел за добу
  python main.py week ru    — + огляд російських джерел за тиждень
  python main.py day all    — + обидва: спочатку українське зведення, потім огляд рос. джерел
"""
import asyncio
import logging
import sys

from digest import build_digest
from scraper import run_tg_scraper
from summarizer import analyze_pending_posts, ensure_indexes


async def main() -> None:
    await ensure_indexes()
    await run_tg_scraper()
    await analyze_pending_posts(chunk=20)

    args = sys.argv[1:]
    if args and args[0] in ("day", "week"):
        if "all" in args[1:]:
            sides = ["ua", "ru"]
        elif "ru" in args[1:]:
            sides = ["ru"]
        else:
            sides = ["ua"]
        for side in sides:
            for part in await build_digest(args[0], side=side):
                print(part, "\n" + "-" * 40)
            print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    asyncio.run(main())
