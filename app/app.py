import os
import asyncio
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from operator import attrgetter
from contextlib import asynccontextmanager

import jwt
from fastapi import FastAPI, APIRouter, Query, WebSocket, WebSocketDisconnect, Response, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext

from models import User, Token, Conversation, Msg
from rmq import consume_msgs, publish_msgs
from config import mongo_client, redis_client, chat_db

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s : %(name)s : %(levelname)s : %(message)s")
logger = logging.getLogger(__name__)

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "1fa95e03b04dab9baf5c96fdcc5f5e4ee5aae170d829c6d72feeb7acb9ec9eb9")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="chat/token")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    mongo_client.close()
    redis_client.close()


app = FastAPI()


async def read_user(username: str) -> User | None:
    res = await chat_db.users.find_one({"username": username})
    return User.model_validate(res) if res else None


def set_user_online_status(username: str, online: bool):
    val = "1" if online else "0"
    redis_client.set(f"user:{username}:online", val, ex=60)


def create_token(data: dict, expires_delta: timedelta) -> str:
    expire_time = datetime.now(timezone.utc) + expires_delta
    return jwt.encode({**data, "exp": expire_time}, JWT_SECRET_KEY, algorithm="HS512")


async def validate_user_creds(username: str, password: str) -> bool:
    if not (user := await read_user(username)):
        return False
    return pwd_context.verify(password, user.password.get_secret_value())


async def get_current_user(token: str = Depends(oauth2_scheme)) -> User | None:
    payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS512"])
    username = payload.get("sub")
    if not username or not (user := await read_user(username)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Error validating user credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


chat_router = APIRouter()


@chat_router.get("", response_class=HTMLResponse)
def chat():
    file_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(file_path, "r") as file:
        html = file.read()
    return HTMLResponse(content=html)


@chat_router.get("/conversation-id")
async def get_conversation_id(usernames: str = Query(..., description="Comma delimited list of usernames")):
    usernames_list = usernames.split(",")
    if len(usernames_list) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least two usernames must be provided"
        )
    for username in usernames_list:
        if not await read_user(username):
            raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{username} is not an existing user"
        )
    sorted_usernames = sorted(usernames_list)
    if res := await chat_db.conversations.find_one({"usernames": sorted_usernames}):
        conversation = Conversation.model_validate(res)
        return conversation.conversation_id
    else:
        conversation_id = hashlib.sha256(",".join(sorted_usernames).encode()).hexdigest()
        conversation = Conversation(conversation_id=conversation_id, usernames=sorted_usernames)
        await chat_db.conversations.insert_one(conversation.model_dump())
        return conversation_id


@chat_router.get("/history/{conversation_id}")
async def get_chat_history(conversation_id: str, _: Token = Depends(oauth2_scheme)):
    cur = chat_db.msgs.find({"conversation_id": conversation_id})
    msgs = [Msg.model_validate(c) for c in await cur.to_list(None)]
    msgs.sort(key=attrgetter("msg_id"))
    return {"msgs": msgs}


@chat_router.post("/logout")
async def logout_user(resp: Response, user: User = Depends(get_current_user)):
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Username does not exist. Error logging out user.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    set_user_online_status(user.username, online=False)
    resp.status_code = 204
    return resp


@chat_router.post("/token", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()) -> Token:
    if not await validate_user_creds(form_data.username, form_data.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Error validating user credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    logger.debug(f"Validated the user credentials for {form_data.username}")
    token_expires_delta = timedelta(minutes=60*24*7)
    token = create_token({"sub": form_data.username}, token_expires_delta)
    logger.debug(f"Created the token for {form_data.username}")
    set_user_online_status(form_data.username, online=True)
    return Token(token=token, token_type="bearer")


users_router = APIRouter()


@users_router.get("/{username}", response_model=User)
async def search_user(username: str, _: User = Depends(get_current_user)):
    return await read_user(username)


@users_router.get("/me", response_model=User)
def get_users_me(user: User = Depends(get_current_user)):
    return user


@users_router.post("", response_model=User)
async def create_user(user_data: User, resp: Response):
    res = await chat_db.users.insert_one(user_data.model_dump())
    logger.debug(f"Result of user write: {res}")
    resp.status_code = 201
    return user_data


ws_router = APIRouter()


async def validate_ws_connection(websocket: WebSocket):
    await websocket.accept()
    try:
        token = await asyncio.wait_for(websocket.receive_text(), timeout=5)
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS512"])
        username = payload.get("sub")
        return user if username and (user := await read_user(username)) else None
    except TimeoutError:
        logger.error("Timed out waiting for token from client")
    except Exception as err:
        logger.error(f"Error validating user token: {err}")
    return None


@ws_router.websocket("/send/{conversation_id}")
async def connect_send(websocket: WebSocket, conversation_id: str):
    if not (user := await validate_ws_connection(websocket)):
        return
    logger.debug(f"conversation ID: {conversation_id}")
    await publish_msgs(websocket, conversation_id, user.username)


@ws_router.websocket("/rcv/{conversation_id}")
async def connect_rcv(websocket: WebSocket, conversation_id: str):
    if not (user := await validate_ws_connection(websocket)):
        return
    logger.debug(f"conversation ID: {conversation_id}")
    await consume_msgs(websocket, conversation_id, user.username)


@ws_router.websocket("/online")
async def connect_online(websocket: WebSocket):
    if not (user := await validate_ws_connection(websocket)):
        return
    try:
        while True:
            logger.debug("waiting for online status ping")
            await asyncio.wait_for(websocket.receive_text(), timeout=30)
            logger.debug("received ping")
            set_user_online_status(user.username, online=True)
    except (TimeoutError, WebSocketDisconnect):
        set_user_online_status(user.username, online=False)


app.include_router(chat_router, prefix="/chat")
app.include_router(users_router, prefix="/users")
app.include_router(ws_router, prefix="/ws")
