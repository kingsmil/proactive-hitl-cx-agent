from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv
load_dotenv()

import db
from api.routes import chat, inbox, actions, sse, settings, telegram, demo, orders, rules
from poller import start_poller, stop_poller


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    start_poller()
    yield
    stop_poller()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(chat.router)
app.include_router(inbox.router)
app.include_router(actions.router)
app.include_router(sse.router)
app.include_router(settings.router)
app.include_router(telegram.router)
app.include_router(demo.router)
app.include_router(orders.router)
app.include_router(rules.router)
