import os
import asyncio
import urllib.parse
import logging
from typing import Any, Callable, Awaitable

from aio_pika import connect_robust, Message
import websockets
from websockets import WebSocketServerProtocol

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger()


WS_HOST = os.getenv("WS_HOST", "localhost")
RMQ_HOST = os.getenv("RMQ_HOST", "localhost")
RMQ_PORT = os.getenv("RMQ_PORT", "")
RMQ_USER = os.getenv("RMQ_USER", "guest")
RMQ_PASSWORD = os.getenv("RMQ_PASSWORD", "guest")
SEND_PORT = os.getenv("SEND_PORT", 8765)
RCV_PORT = os.getenv("RCV_PORT", 8764)


def parse_query_params(url: str) -> tuple[str, str] | None:
    query_params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    user = query_params.get("username", [None])[0]
    friend = query_params.get("friend", [None])[0]

    if not user:
        logger.error(f"Missing username for user. Cannot establish chat connection.")
        return None
    if not friend:
        logger.error(f"Missing username for friend. Cannot establish chat connection.")
        return None
    
    logger.info(f"Handling connection for user: {user}, friend: {friend}")
    return user, friend


def get_rmq_connection_str():
    if RMQ_PORT:
        return f"amqp://{RMQ_USER}:{RMQ_PASSWORD}@{RMQ_HOST}:{RMQ_PORT}/"
    else:
        return f"amqp://{RMQ_USER}:{RMQ_PASSWORD}@{RMQ_HOST}/"


async def consume_msgs(websocket: WebSocketServerProtocol, path: str) -> None:
    if res := parse_query_params(path):
        user, friend = res
    else:
        return
    
    conn_str = get_rmq_connection_str()
    conn = await connect_robust(conn_str)
    channel = await conn.channel()

    queue_name = f"{friend}:{user}"
    queue = await channel.declare_queue(queue_name)

    async with queue.iterator() as queue_iter:
        async for msg in queue_iter:
            async with msg.process():
                await websocket.send(msg.body.decode())
                logger.debug(f"Consumed message: {msg}")

    await conn.close()


async def publish_msgs(websocket: WebSocketServerProtocol, path: str) -> None:
    if res := parse_query_params(path):
        user, friend = res
    else:
        return

    conn_str = get_rmq_connection_str()
    conn = await connect_robust(conn_str)
    channel = await conn.channel()

    queue_name = f"{user}:{friend}"
    await channel.declare_queue(queue_name)

    async for msg in websocket:
        await channel.default_exchange.publish(
            Message(body=msg.encode()),  # type: ignore
            routing_key=queue_name
        )
        logger.debug(f"Published message: {msg} to queue: {queue_name}")

    await conn.close()


async def handle_connection(handler: Callable[[WebSocketServerProtocol, str], Awaitable[Any]], port: int):
    async with websockets.serve(handler, WS_HOST, port):
        logger.info(f"Websocket started serving on port {port}")
        await asyncio.Future()


async def main():
    tasks = [handle_connection(publish_msgs, int(SEND_PORT)), handle_connection(consume_msgs, int(RCV_PORT))]
    await asyncio.gather(*tasks)
    

if __name__ == "__main__":
    asyncio.run(main())
    