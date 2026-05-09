from dataclasses import dataclass
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
CACHE_ROOT_DIR = DATA_DIR / "cache"
CACHE_DIR = CACHE_ROOT_DIR / "images"
AWS_DB_PATH = DATA_DIR / "aws_app.db"
AWS_CACHE_DIR = CACHE_ROOT_DIR / "aws-images"

SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1mfeHbvlI0CrT54WUC_stFTuXqg4a9a7YeVqZ5wHFbo8/"
    "export?format=csv&gid=514978031"
)

@dataclass(frozen=True)
class DatasetConfig:
    slug: str
    label: str
    page_path: str
    api_prefix: str
    db_path: Path
    cache_dir: Path
    aws_profile: str
    aws_region: str
    s3_bucket: str
    batch_prefixes: list[tuple[str, str]]


WASABI_BATCH_RANGE_START = 11
WASABI_BATCH_RANGE_END = 50
WASABI_BATCH_PREFIXES = [
    (f"batch-{batch_number:02d}", f"batch-{batch_number:02d}/")
    for batch_number in range(WASABI_BATCH_RANGE_START, WASABI_BATCH_RANGE_END + 1)
]

AWS_BATCH_PREFIXES = [
    ("panoramic_clean", "panoramic_clean/"),
    ("batch2", "batch2/"),
    *[(f"batch-{batch_number:02d}", f"batch-{batch_number:02d}/") for batch_number in range(3, 11)],
]

WASABI_DATASET = DatasetConfig(
    slug="wasabi",
    label="Wasabi Runs",
    page_path="/",
    api_prefix="/api",
    db_path=DB_PATH,
    cache_dir=CACHE_DIR,
    aws_profile="wasabi",
    aws_region="us-west-1",
    s3_bucket="pano-processed-runs",
    batch_prefixes=WASABI_BATCH_PREFIXES,
)

AWS_DATASET = DatasetConfig(
    slug="aws",
    label="AWS Legacy Runs",
    page_path="/aws",
    api_prefix="/aws/api",
    db_path=AWS_DB_PATH,
    cache_dir=AWS_CACHE_DIR,
    aws_profile="s3",
    aws_region="us-east-1",
    s3_bucket="aipanoexport-batch2",
    batch_prefixes=AWS_BATCH_PREFIXES,
)

DATASETS = (WASABI_DATASET, AWS_DATASET)

DEFAULT_IMAGE_COUNT = 3
JPEG_QUALITY = 75
