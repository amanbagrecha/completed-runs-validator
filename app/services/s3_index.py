import re
from dataclasses import dataclass
from datetime import datetime

import boto3

from app.config import DatasetConfig


TAR_NAME_RE = re.compile(r"([^/]+)\.tar(?:\.gz)?$", re.IGNORECASE)


@dataclass(frozen=True)
class S3RunObject:
    run_id: str
    batch_name: str
    prefix: str
    key: str
    size: int | None
    last_modified: datetime | None


def get_s3_client(dataset: DatasetConfig):
    session = boto3.Session(profile_name=dataset.aws_profile, region_name=dataset.aws_region)
    return session.client("s3", region_name=dataset.aws_region)


def list_run_tars(dataset: DatasetConfig) -> dict[str, S3RunObject]:
    client = get_s3_client(dataset)
    runs: dict[str, S3RunObject] = {}
    for batch_name, prefix in dataset.batch_prefixes:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=dataset.s3_bucket, Prefix=prefix, Delimiter="/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                match = TAR_NAME_RE.search(key)
                if not match:
                    continue
                run_id = match.group(1)
                runs[run_id] = S3RunObject(
                    run_id=run_id,
                    batch_name=batch_name,
                    prefix=prefix,
                    key=key,
                    size=obj.get("Size"),
                    last_modified=obj.get("LastModified"),
                )
    return runs
