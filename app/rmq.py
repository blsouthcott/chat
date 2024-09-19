import os
import logging
import asyncio
from datetime import datetime, UTC

from aio_pika import connect_robust, Message
from fastapi import WebSocket, WebSocketDisconnect

from models import Msg, Conversation
from config import chat_db, redis_client


logger = logging.getLogger(__name__)


RMQ_HOST = os.getenv("RMQ_HOST", "localhost")
RMQ_PORT = os.getenv("RMQ_PORT", "")
RMQ_USER = os.getenv("RMQ_USER", "guest")
RMQ_PASSWORD = os.getenv("RMQ_PASSWORD", "guest")


def get_rmq_connection_str() -> str:
    if RMQ_PORT:
        return f"amqp://{RMQ_USER}:{RMQ_PASSWORD}@{RMQ_HOST}:{RMQ_PORT}/"
    else:
        return f"amqp://{RMQ_USER}:{RMQ_PASSWORD}@{RMQ_HOST}/"
    

async def rmq_connect(conversation_id: str, usernames: list[str]):
    conn_str = get_rmq_connection_str()
    conn = await connect_robust(conn_str)
    channel = await conn.channel()
    logger.debug([f"{conversation_id}:{u}" for u in usernames])
    queues = {u: await channel.declare_queue(f"{conversation_id}:{u}") for u in usernames}
    return conn, channel, queues


async def publish_msgs(websocket: WebSocket, conversation_id: str, sender: str) -> None:
    try:
        res = await chat_db.conversations.find_one({"conversation_id": conversation_id})
        # TODO: handle error reading from db
        conversation = Conversation.model_validate(res)
        recipient_usernames = [u for u in conversation.usernames if u != sender]
        conn, channel, queues = await rmq_connect(conversation_id, recipient_usernames)

        while True:
            msg_content = await websocket.receive_text()
            msg_id = redis_client.incr("global:msg_id")
            msg = Msg(
                msg_id=msg_id,
                conversation_id=conversation_id, 
                sender_username=sender,
                content=msg_content,
                sent_timestamp=datetime.now(UTC).timestamp()
            )
            await chat_db.msgs.insert_one(msg.model_dump())
            
            online_status = {u: redis_client.get(f"user:{u}:online") for u in queues}
            tasks = [
                channel.default_exchange.publish(
                    Message(
                        body=msg_content.encode(),
                        headers={
                            "msg_id": msg_id,  # type: ignore
                            "conversation_id": conversation_id,
                            "sender_username": msg.sender_username, 
                            "sent_timestamp": msg.sent_timestamp
                        }
                    ),
                    routing_key=q.name
                )
                for u, q in queues.items() if online_status[u] == "1"
            ]
            await asyncio.gather(*tasks)
            logger.debug(f"Published message: {msg_content} to queues: {', '.join(q.name for q in queues)}")

    except WebSocketDisconnect as err:
        logger.debug("Closing RMQ connection due to websocket disconnect")
    except Exception as err:
        logger.debug(f"Closing RMQ connection due to error: {err}")
    finally:
        await channel.close()
        await conn.close()


async def consume_msgs(websocket: WebSocket, conversation_id: str, recipient: str) -> None:    
    try:
        conn, channel, queues = await rmq_connect(conversation_id, [recipient])
        async with queues[0].iterator() as queue_iter:
            async for msg in queue_iter:
                async with msg.process():
                    msg_body = msg.body.decode()
                    msg = Msg(content=msg_body, **msg.headers)
                    # msg_json = json.dumps({"content": msg_body, "sender_username": sender, "sent_timestamp": sent_time})
                    msg_json = msg.model_dump_json()
                    await websocket.send_text(msg_json)
                    logger.debug(f"Consumed message: {msg} in queue: {queues[0].name}")
    
    except WebSocketDisconnect as err:
        logger.debug("Closing RMQ connection due to websocket disconnect")
    except Exception as err:
        logger.debug(f"Closing RMQ connection due to error: {err}")
    finally:
        await channel.close()
        await conn.close()
