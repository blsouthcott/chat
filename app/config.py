import os
import motor.motor_asyncio
import redis

MONGO_DB_CONN_STR = os.getenv("MONGODB_CONN_STR", "mongodb://localhost:27017")
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_DB_CONN_STR)
chat_db: motor.motor_asyncio.AsyncIOMotorDatabase = mongo_client.chat_db

REDIS_CONN_STR = os.getenv("REDIS_CONN_STR", "redis://localhost:6379")
redis_client = redis.from_url(REDIS_CONN_STR, decode_responses=True)
