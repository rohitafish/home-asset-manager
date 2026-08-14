import os

from dotenv import load_dotenv
from sqlmodel import Session, create_engine

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://assetmgt:assetmgt@localhost:5432/assetmgt"
)

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)


def get_session():
    with Session(engine) as session:
        yield session
