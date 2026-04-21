import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://gw.promasch.in"
LOGIN_USER = os.getenv(
    "INDENT_USER",
    os.getenv("PROMASCH_USER", "Vikram@greenwave.ws"),
)
LOGIN_PASSWORD = os.getenv(
    "INDENT_PASSWORD",
    os.getenv("PROMASCH_PASSWORD", "Infosys@9009"),
)

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
AWS_BUCKET_NAME = os.getenv("AWS_BUCKET_NAME", "promasch-indents")

# Optional. Keep empty by default because many Indent flows do not expose a
# direct PDF URL in list/detail payloads.
#
# Example if available in your environment:
#   INDENT_PDF_URL_TEMPLATE="https://gw.promasch.in/IndentPdf?indentId={indent_id}"
INDENT_PDF_URL_TEMPLATE = os.getenv("INDENT_PDF_URL_TEMPLATE", "")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
RUNS_DIR = DATA_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)

INDENT_CATALOG_FILE = DATA_DIR / "indent_catalog.json"
FAILED_IDS_FILE = DATA_DIR / "failed_ids.json"
FINAL_OUTPUT_FILE = DATA_DIR / "final_output.json"
LAST_RUN_FILE = DATA_DIR / "last_run.txt"

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "10"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.1"))
PDF_TIMEOUT = int(os.getenv("PDF_TIMEOUT", "30"))
PDF_RETRIES = int(os.getenv("PDF_RETRIES", "3"))
