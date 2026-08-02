import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CS2CAP_API_KEY = os.environ.get("CS2CAP_API_KEY")
