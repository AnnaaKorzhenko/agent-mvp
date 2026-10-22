import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI не задано у файлі .env")

client = AsyncIOMotorClient(MONGO_URI, tz_aware=True)
db = client["News"]

sources_collection = db["sources"]
posts_collection = db["posts"]
digests_collection = db["digests"]
