from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import db
from api.routes import chat, inbox, actions, sse, settings, whatsapp

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.on_event("startup")
def startup():
    db.init_db()

# Register modular routes
app.include_router(chat.router)
app.include_router(inbox.router)
app.include_router(actions.router)
app.include_router(sse.router)
app.include_router(settings.router)
app.include_router(whatsapp.router)
