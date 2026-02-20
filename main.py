# Entry point — keeps uvicorn invocation simple:
#   uvicorn main:app --reload --port 8000
import logging
from dotenv import load_dotenv
load_dotenv()  # loads .env before any module reads os.environ

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Keep third-party chatter at WARNING so only our logs stand out
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("fastapi").setLevel(logging.WARNING)

from api.app import app  # noqa: F401
