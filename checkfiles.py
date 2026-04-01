#!/usr/bin/env python3
import argparse
import csv
import shutil
import os
import sys
from datetime import date, datetime
from pathlib import Path

from PIL import ExifTags, Image

CSV_NAME = "photo_index.csv"
CSV_COLUMNS = ("filename", "filepath", "date_taken")
EXTS = {".jpg", ".jpeg", ".nef"}
CHECK_DATE_MARGIN_DAYS = 3


def photo_date(path: Path):
    try:
        img = Image.open(path)
        exif = img.getexif()
        if not exif:
            return None

        tags = {ExifTags.TAGS.get(key, key): value for key, value in exif.items()}
        for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
            if key in tags:
                return datetime.strptime(tags[key], "%Y:%m:%d %H:%M:%S").date()
    except Exception:
        return None
    return None


def normalize_relative_path(path: Path) -> str:
    if path == Path("."):
        return "."
    return path.as_posix()


def load_existing_rows(csv_path: Path):
    rows = []
    analyzed_dirs = set()

    if not csv_path.exists():
        return rows, analyzed_dirs

    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            filepath = (row.get("filepath") or "").strip()
            if not filepath:
                continue

            normalized_path = normalize_relative_path(Path(filepath))
            rows.append(
                {
                    "filename": (row.get("filename") or Path(normalized_path).name).strip(),
                    "filepath": normalized_path,
                    "date_taken": (row.get("date_taken") or "").strip(),
                }
            )
            analyzed_dirs.add(normalize_relative_path(Path(normalized_path).parent))

    return rows, analyzed_dirs


def iter_photo_rows(root: Path, analyzed_dirs: set[str], csv_name: str):
    for current_dir, dirnames, filenames in os.walk(root):
        current_path = Path(current_dir)
        relative_dir = normalize_relative_path(current_path.relative_to(root))

        if relative_dir in analyzed_dirs:
            dirnames[:] = []
            continue

        dirnames[:] = [
            dirname
            for dirname in dirnames
            if normalize_relative_path((current_path / dirname).relative_to(root)) not in analyzed_dirs
        ]

        for filename in sorted(filenames):
            if current_path == root and filename == csv_name:
                continue

            file_path = current_path / filename
            if file_path.suffix.lower() not in EXTS:
                continue

            taken_on = photo_date(file_path)
            yield {
                "filename": file_path.name,
                "filepath": normalize_relative_path(file_path.relative_to(root)),
                "date_taken": taken_on.isoformat() if taken_on else "",
            }


def write_rows(csv_path: Path, rows):
    rows.sort(key=lambda row: row["filepath"])
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def parse_cli_args():
    parser = argparse.ArgumentParser(description="Build or query a photo CSV index.")
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Folder to index or folder containing photo_index.csv",
    )
    parser.add_argument(
        "--check-date",
        dest="check_date",
        help="Find CSV entries near a date (supports MM-DD, MM/DD, YYYY-MM-DD, YYYY/MM/DD)",
    )
    parser.add_argument(
        "--margin",
        type=int,
        default=None,
        help="Day margin for --check-date matching (only valid with --check-date)",
    )
    parser.add_argument(
        "--copy-to",
        dest="copy_to",
        help="Absolute destination folder to copy --check-date matches to (only valid with --check-date)",
    )
    return parser.parse_args()


def parse_target_date(value: str) -> date:
    formats = ("%m-%d", "%m/%d", "%Y-%m-%d", "%Y/%m/%d")
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).date()
            if "%Y" in fmt:
                return date(2000, parsed.month, parsed.day)
            return date(2000, parsed.month, parsed.day)
        except ValueError:
            continue
    raise ValueError(
        "Invalid --check-date value. Use MM-DD, MM/DD, YYYY-MM-DD, or YYYY/MM/DD."
    )


def parse_csv_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def is_within_margin(day_a: date, day_b: date, margin_days: int) -> bool:
    ordinal_a = day_a.timetuple().tm_yday
    ordinal_b = day_b.timetuple().tm_yday
    delta = abs(ordinal_a - ordinal_b)
    wrapped_delta = 366 - delta
    return min(delta, wrapped_delta) <= margin_days


def conflict_safe_destination(copy_to_path: Path, year: int, extension: str) -> Path:
    index = 1
    while True:
        candidate = copy_to_path / f"{year}-{index}{extension}"
        if not candidate.exists():
            return candidate
        index += 1


def check_date_mode(csv_path: Path, target_date_value: str, margin_days: int, copy_to_path: Path | None):
    if not csv_path.exists():
        print(f"CSV file not found: {csv_path}")
        return 1

    target = parse_target_date(target_date_value)
    rows, _ = load_existing_rows(csv_path)

    matches = []
    for row in rows:
        taken_on = parse_csv_date(row["date_taken"])
        if not taken_on:
            continue

        normalized_taken = date(2000, taken_on.month, taken_on.day)
        if is_within_margin(normalized_taken, target, margin_days):
            matches.append(row)

    print(
        f"Found {len(matches)} matching entries within {margin_days} days of "
        f"{target.month:02d}-{target.day:02d}:"
    )

    copied = 0
    for row in matches:
        absolute_path = (csv_path.parent / row["filepath"]).resolve()
        print(f"{absolute_path}, {row['date_taken']}")

        if copy_to_path is not None and absolute_path.exists():
            taken_on = parse_csv_date(row["date_taken"])
            year = taken_on.year if taken_on is not None else datetime.now().year
            destination = conflict_safe_destination(copy_to_path, year, absolute_path.suffix)
            shutil.copy2(absolute_path, destination)
            copied += 1

    if copy_to_path is not None:
        print(f"Copied {copied} files to {copy_to_path}")
    return 0


def main():
    args = parse_cli_args()
    root = Path(args.root).resolve()
    csv_path = root / CSV_NAME

    if (args.margin is not None or args.copy_to) and not args.check_date:
        print("--margin and --copy-to can only be used together with --check-date")
        return 2

    effective_margin = args.margin if args.margin is not None else CHECK_DATE_MARGIN_DAYS
    if effective_margin < 0:
        print("--margin must be >= 0")
        return 2

    copy_to_path = None
    if args.copy_to:
        copy_to_path = Path(args.copy_to)
        if not copy_to_path.is_absolute():
            print("--copy-to must be an absolute path")
            return 2
        copy_to_path.mkdir(parents=True, exist_ok=True)

    if args.check_date:
        try:
            return check_date_mode(csv_path, args.check_date, effective_margin, copy_to_path)
        except ValueError as exc:
            print(str(exc))
            return 2

    existing_rows, analyzed_dirs = load_existing_rows(csv_path)
    new_rows = list(iter_photo_rows(root, analyzed_dirs, csv_path.name))
    all_rows = existing_rows + new_rows

    write_rows(csv_path, all_rows)

    print(f"Wrote {len(all_rows)} entries to {csv_path}")
    print(f"Loaded {len(existing_rows)} existing entries")
    print(f"Added {len(new_rows)} new entries")
    print(f"Skipped {len(analyzed_dirs)} previously indexed folders")
    return 0


if __name__ == "__main__":
    sys.exit(main())