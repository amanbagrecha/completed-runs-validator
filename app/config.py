from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = DATA_DIR / "cache" / "images"
DB_PATH = DATA_DIR / "app.db"

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1mfeHbvlI0CrT54WUC_stFTuXqg4a9a7YeVqZ5wHFbo8/"
    "export?format=csv&gid=2085465220"
)

AWS_PROFILE = "s3"
AWS_REGION = "us-east-1"
S3_BUCKET = "aipanoexport-batch2"

BATCH_PREFIXES = [
    ("panoramic_clean", "panoramic_clean/"),
    ("batch2", "batch2/"),
    ("batch-03", "batch-03/"),
    ("batch-04", "batch-04/"),
    ("batch-05", "batch-05/"),
    ("batch-06", "batch-06/"),
    ("batch-07", "batch-07/"),
    ("batch-08", "batch-08/"),
    ("batch-09", "batch-09/"),
    ("batch-10", "batch-10/"),
]

DEFAULT_IMAGE_COUNT = 3
JPEG_QUALITY = 75
