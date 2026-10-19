import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI не задано у файлі .env")

MONGO_DB_NAME = (os.getenv("MONGO_DB_NAME") or "").strip() or "News-data"

client = AsyncIOMotorClient(MONGO_URI)
db = client[MONGO_DB_NAME]

sources_collection = db["sources"]
posts_collection = db["raw_posts"]
digests_collection = db["digests"]
