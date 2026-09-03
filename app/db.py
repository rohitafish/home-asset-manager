import os

from dotenv import load_dotenv
from sqlmodel import Session, create_engine

load_dotenv()

# No credentials in the fallback: the real URL comes from .env (see
# .env.example), and app/main.py refuses to start without it. The
# credential-free placeholder only exists so importing this module -- which
# the test suite does with the engine swapped out -- doesn't need a URL.
DATABASE_URL = os.environ.get("DATABASE_URL") or "postgresql+psycopg://localhost:5432/assetmgt"

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)


def get_session():
    with Session(engine) as session:
        yield session
