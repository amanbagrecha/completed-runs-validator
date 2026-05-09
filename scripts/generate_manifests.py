from __future__ import annotations

import argparse
import json
import tarfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


DEFAULT_BUCKET = "aipanoexport-batch2"
DEFAULT_PROFILE = "s3"
DEFAULT_REGION = "us-east-1"
DEFAULT_PREFIXES = ["panoramic_clean/", "batch2/"] + [f"batch-{index:02d}/" for index in range(3, 11)]
DEFAULT_TEMP_DIR = Path("/tmp/opencode/manifest_backfill")


@dataclass(frozen=True)
class TarObject:
    prefix: str
    key: str
    manifest_key: str
    size: int


@dataclass(frozen=True)
class ProcessResult:
    status: str
    key: str
    manifest_key: str
    file_count: int = 0
    duration_seconds: float = 0.0
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill missing tar manifests into S3")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--prefix", action="append", dest="prefixes")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--temp-dir", type=Path, default=DEFAULT_TEMP_DIR)
    return parser.parse_args()


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def make_s3_client(profile: str, region: str, max_pool_connections: int) -> object:
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client(
        "s3",
        region_name=region,
        config=Config(max_pool_connections=max_pool_connections),
    )


def manifest_key_for_tar(tar_key: str) -> str:
    lower = tar_key.lower()
    if lower.endswith(".tar.gz"):
        return tar_key[:-7] + ".manifest.json"
    if lower.endswith(".tar"):
        return tar_key[:-4] + ".manifest.json"
    raise ValueError(f"Unsupported tar key: {tar_key}")


def run_id_from_tar_key(tar_key: str) -> str:
    name = Path(tar_key).name
    if name.lower().endswith(".tar.gz"):
        return name[:-7]
    if name.lower().endswith(".tar"):
        return name[:-4]
    raise ValueError(f"Unsupported tar key: {tar_key}")


def list_missing_manifest_tars(client, bucket: str, prefixes: list[str]) -> list[TarObject]:
    missing: list[TarObject] = []
    for prefix in prefixes:
        log(f"Scanning prefix {prefix}")
        objects: dict[str, int] = {}
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                objects[obj["Key"]] = int(obj["Size"])

        for key, size in sorted(objects.items()):
            if not key.lower().endswith(".tar"):
                continue
            manifest_key = manifest_key_for_tar(key)
            if manifest_key in objects:
                continue
            missing.append(TarObject(prefix=prefix, key=key, manifest_key=manifest_key, size=size))

        log(
            f"Prefix {prefix} has {sum(1 for item in missing if item.prefix == prefix)} missing manifests so far"
        )

    return missing


def manifest_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def build_manifest(client, bucket: str, tar_object: TarObject) -> dict[str, object]:
    files: list[dict[str, int | str]] = []
    response = client.get_object(Bucket=bucket, Key=tar_object.key)
    with closing(response["Body"]):
        with tarfile.open(fileobj=response["Body"], mode="r|*") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                if member.offset_data is None:
                    raise RuntimeError(f"Missing offset_data for {member.name} in {tar_object.key}")
                files.append(
                    {
                        "name": member.name,
                        "size": int(member.size),
                        "tar_offset": int(member.offset_data),
                    }
                )

    return {
        "run_id": run_id_from_tar_key(tar_object.key),
        "file_count": len(files),
        "tar_bytes": tar_object.size,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }


def write_manifest_file(temp_dir: Path, manifest_key: str, manifest: dict[str, object]) -> Path:
    path = temp_dir / manifest_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def process_tar_object(
    tar_object: TarObject,
    *,
    bucket: str,
    profile: str,
    region: str,
    temp_dir: Path,
    dry_run: bool,
    max_pool_connections: int,
) -> ProcessResult:
    started_at = time.monotonic()
    client = make_s3_client(profile, region, max_pool_connections=max_pool_connections)

    if manifest_exists(client, bucket, tar_object.manifest_key):
        return ProcessResult(
            status="skipped",
            key=tar_object.key,
            manifest_key=tar_object.manifest_key,
            duration_seconds=time.monotonic() - started_at,
        )

    manifest_path: Path | None = None
    try:
        manifest = build_manifest(client, bucket, tar_object)
        file_count = int(manifest["file_count"])
        if dry_run:
            return ProcessResult(
                status="dry-run",
                key=tar_object.key,
                manifest_key=tar_object.manifest_key,
                file_count=file_count,
                duration_seconds=time.monotonic() - started_at,
            )

        manifest_path = write_manifest_file(temp_dir, tar_object.manifest_key, manifest)
        client.upload_file(
            str(manifest_path),
            bucket,
            tar_object.manifest_key,
            ExtraArgs={"ContentType": "application/json"},
        )
        manifest_path.unlink(missing_ok=True)
        return ProcessResult(
            status="created",
            key=tar_object.key,
            manifest_key=tar_object.manifest_key,
            file_count=file_count,
            duration_seconds=time.monotonic() - started_at,
        )
    except Exception as exc:
        if manifest_path is not None:
            manifest_path.unlink(missing_ok=True)
        return ProcessResult(
            status="failed",
            key=tar_object.key,
            manifest_key=tar_object.manifest_key,
            duration_seconds=time.monotonic() - started_at,
            error=str(exc),
        )


def format_gib(size: int) -> str:
    return f"{size / (1024 ** 3):.2f} GiB"


def main() -> int:
    args = parse_args()
    prefixes = args.prefixes or DEFAULT_PREFIXES
    temp_dir = args.temp_dir
    temp_dir.mkdir(parents=True, exist_ok=True)

    max_pool_connections = max(4, args.workers * 2)
    client = make_s3_client(args.profile, args.region, max_pool_connections=max_pool_connections)
    missing = list_missing_manifest_tars(client, args.bucket, prefixes)
    if args.limit is not None:
        missing = missing[: args.limit]

    total_bytes = sum(item.size for item in missing)
    log(
        f"Discovered {len(missing)} missing manifests in {args.bucket}; "
        f"tar bytes to scan: {total_bytes} ({format_gib(total_bytes)})"
    )
    if not missing:
        log("Nothing to do")
        return 0

    counts: Counter[str] = Counter()
    start = time.monotonic()

    if args.workers == 1:
        for index, tar_object in enumerate(missing, start=1):
            result = process_tar_object(
                tar_object,
                bucket=args.bucket,
                profile=args.profile,
                region=args.region,
                temp_dir=temp_dir,
                dry_run=args.dry_run,
                max_pool_connections=max_pool_connections,
            )
            counts[result.status] += 1
            if result.status == "failed":
                log(f"[{index}/{len(missing)}] failed {result.key}: {result.error}")
            else:
                log(
                    f"[{index}/{len(missing)}] {result.status} {result.manifest_key} "
                    f"files={result.file_count} duration={result.duration_seconds:.1f}s"
                )
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    process_tar_object,
                    tar_object,
                    bucket=args.bucket,
                    profile=args.profile,
                    region=args.region,
                    temp_dir=temp_dir,
                    dry_run=args.dry_run,
                    max_pool_connections=max_pool_connections,
                ): tar_object
                for tar_object in missing
            }
            for index, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                counts[result.status] += 1
                if result.status == "failed":
                    log(f"[{index}/{len(missing)}] failed {result.key}: {result.error}")
                else:
                    log(
                        f"[{index}/{len(missing)}] {result.status} {result.manifest_key} "
                        f"files={result.file_count} duration={result.duration_seconds:.1f}s"
                    )

    elapsed = time.monotonic() - start
    log(
        "Finished backfill: "
        + ", ".join(f"{status}={counts[status]}" for status in sorted(counts))
        + f", elapsed={elapsed / 60:.1f}m"
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
