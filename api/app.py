from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import db
from api.routes import chat, inbox, actions, sse, settings

from contextlib import asynccontextmanager
from poller import start_poller, stop_poller

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    db.init_db()
    start_poller()
    yield
    # Shutdown
    stop_poller()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(chat.router)
app.include_router(inbox.router)
app.include_router(actions.router)
app.include_router(sse.router)
app.include_router(settings.router)
