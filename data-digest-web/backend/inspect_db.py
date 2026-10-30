"""
Показує структуру бази, щоб налаштувати app/config.py.
Запуск:  python inspect_db.py
Нічого не змінює в базі — лише читає.
"""
import asyncio

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import COLLECTIONS, POST_FIELDS, STORY_FIELDS, settings


def describe(doc, prefix="", depth=0):
    for k, v in doc.items():
        name = f"{prefix}{k}"
        t = type(v).__name__
        preview = repr(v)
        if len(preview) > 70:
            preview = preview[:67] + "..."
        print(f"    {name:<30} {t:<10} {preview}")
        if isinstance(v, dict) and depth < 1:
            describe(v, prefix=name + ".", depth=depth + 1)


async def main():
    db = AsyncIOMotorClient(settings.MONGODB_URI)[settings.DB_NAME]
    names = await db.list_collection_names()
    print(f"База: {settings.DB_NAME}\nКолекції: {', '.join(sorted(names))}\n")

    for name in sorted(names):
        count = await db[name].estimated_document_count()
        doc = await db[name].find_one(sort=[("_id", -1)])
        print(f"== {name}  ({count} документів)")
        if doc:
            describe(doc)
        print()

    posts = COLLECTIONS["posts"]
    if posts in names:
        print(f"== Значення в {posts} (для CATEGORY_MAP / PLATFORM_MAP / LANG_MAP)")
        for f in ("platform", "lang", "category", "relevance"):
            field = POST_FIELDS[f]
            vals = await db[posts].distinct(field)
            print(f"    {field}: {vals[:30]}")
        print()

    stories = COLLECTIONS["stories"]
    if stories in names:
        vals = await db[stories].distinct(STORY_FIELDS["category"])
        print(f"== {stories}.{STORY_FIELDS['category']}: {vals[:30]}")

    missing = [c for c in COLLECTIONS.values() if c not in names]
    if missing:
        print(f"\nУВАГА: колекцій {missing} немає — виправ COLLECTIONS у app/config.py")


asyncio.run(main())
