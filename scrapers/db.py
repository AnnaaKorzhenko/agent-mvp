import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI не задано у файлі .env")

client = AsyncIOMotorClient(MONGO_URI)
db = client["News-data"]

sources_collection = db["sources"]
posts_collection = db["raw_posts"]