from pydantic import BaseModel, Secret


class User(BaseModel):
    username: str
    password: Secret[str]
    email: str
    first_name: str 
    last_name: str
    middle_name: str
    city: str
    state: str
    country: str
    disabled: bool


class Token(BaseModel):
    token: str
    token_type: str


class Conversation(BaseModel):
    conversation_id: str
    usernames: list[str]


class Msg(BaseModel):
    conversation_id: str
    msg_id: int
    sender_username: str
    content: str
    sent_timestamp: float
