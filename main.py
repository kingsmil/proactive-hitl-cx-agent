# Entry point — keeps uvicorn invocation simple:
#   uvicorn main:app --reload --port 8000
from api.app import app  # noqa: F401
