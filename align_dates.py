#!/usr/bin/env python3
"""
Align picture dates based on a reference picture.

Usage:
    python align_dates.py <folder> <checkpic> <refpic> [--offset SECONDS]

The script:
1. Stores the original date taken of checkpic
2. Compares checkpic's time with refpic's time
3. If different, updates checkpic's date taken to match refpic (with optional offset)
4. Iterates through all pictures in the folder, computing the difference between
   each picture and checkpic's original date, then applies the same offset
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
except ImportError:
    print("Please install Pillow: pip install Pillow")
    sys.exit(1)


def get_exif_date(filepath: str) -> Optional[datetime]:
    """Extract the date taken from EXIF data."""
    try:
        image = Image.open(filepath)
        exif_data = image._getexif()
        if not exif_data:
            return None

        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == "DateTimeOriginal":
                return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except Exception as e:
        print(f"Warning: Could not read EXIF from {filepath}: {e}")

    return None


def set_exif_date(filepath: str, new_date: datetime) -> bool:
    """Set the date taken in EXIF data."""
    try:
        import piexif
    except ImportError:
        print("Please install piexif: pip install piexif")
        return False

    try:
        exif_dict = piexif.load(filepath)
        date_str = new_date.strftime("%Y:%m:%d %H:%M:%S")
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = date_str
        exif_dict["0th"][piexif.ImageIFD.DateTime] = date_str
        piexif.insert(piexif.dump(exif_dict), filepath)
        return True
    except Exception as e:
        print(f"Warning: Could not set EXIF date for {filepath}: {e}")
        return False


def get_time_difference(pic1_date: datetime, pic2_date: datetime) -> timedelta:
    """Calculate the time difference between two pictures."""
    return pic2_date - pic1_date


def find_images(folder: Path) -> list:
    """Find all image files in the folder."""
    extensions = {'.jpg', '.jpeg', '.dng', '.tif', '.tiff'}
    images = []
    for item in sorted(folder.iterdir()):
        if item.is_file() and item.suffix.lower() in extensions:
            images.append(item)
    return images


def main():
    parser = argparse.ArgumentParser(
        description="Align picture dates based on a reference picture."
    )
    parser.add_argument("folder", help="Folder containing the pictures")
    parser.add_argument("checkpic", help="Picture to check and use as reference point")
    parser.add_argument("refpic", help="Reference picture with correct date")
    parser.add_argument("--offset", type=int, default=0,
                        help="Offset in seconds to apply to the reference time")

    args = parser.parse_args()

    folder = Path(args.folder)
    checkpic = folder / args.checkpic
    refpic = folder / args.refpic

    if not folder.exists():
        print(f"Error: Folder {folder} does not exist")
        sys.exit(1)

    if not checkpic.exists():
        print(f"Error: Picture {checkpic} does not exist")
        sys.exit(1)

    if not refpic.exists():
        print(f"Error: Reference picture {refpic} does not exist")
        sys.exit(1)

    # Step 1: Get original date of checkpic
    print(f"Reading original date from {checkpic.name}...")
    checkpic_original_date = get_exif_date(str(checkpic))
    if not checkpic_original_date:
        print(f"Error: Could not read EXIF date from {checkpic.name}")
        sys.exit(1)
    print(f"  Original date: {checkpic_original_date}")

    # Step 2: Get date from refpic
    print(f"Reading reference date from {refpic.name}...")
    refpic_date = get_exif_date(str(refpic))
    if not refpic_date:
        print(f"Error: Could not read EXIF date from {refpic.name}")
        sys.exit(1)
    print(f"  Reference date: {refpic_date}")

    # Step 3: Check if times are different and update checkpic
    offset_delta = timedelta(seconds=args.offset)
    refpic_date_with_offset = refpic_date + offset_delta

    if args.offset != 0:
        print(f"  Applying offset of {args.offset} seconds")
        print(f"  Reference date with offset: {refpic_date_with_offset}")

    time_diff = refpic_date_with_offset - checkpic_original_date
    print(f"  Time difference: {time_diff}")

    # Update checkpic to match refpic
    print(f"Updating {checkpic.name} to match reference date...")
    if set_exif_date(str(checkpic), refpic_date_with_offset):
        print(f"  Successfully updated {checkpic.name}")
    else:
        print(f"  Failed to update {checkpic.name}")
        sys.exit(1)

    # Step 4: Iterate through all pictures in folder
    print(f"\nProcessing all pictures in {folder}...")
    images = find_images(folder)

    if not images:
        print("No images found in folder")
        sys.exit(0)

    print(f"Found {len(images)} images")

    success_count = 0
    fail_count = 0

    for img_path in images:
        if img_path.name == refpic.name:
            print(f"Skipping reference picture: {img_path.name}")
            continue

        print(f"\nProcessing {img_path.name}...")

        original_date = get_exif_date(str(img_path))
        if not original_date:
            print(f"  Warning: Could not read EXIF date, skipping")
            fail_count += 1
            continue

        # Calculate difference from checkpic's original date
        diff_from_checkpic = original_date - checkpic_original_date
        new_date = refpic_date_with_offset + diff_from_checkpic

        print(f"  Original date: {original_date}")
        print(f"  Difference from checkpic: {diff_from_checkpic}")
        print(f"  New date: {new_date}")

        if set_exif_date(str(img_path), new_date):
            print(f"  Successfully updated")
            success_count += 1
        else:
            print(f"  Failed to update")
            fail_count += 1

    print(f"\n{'='*50}")
    print(f"Summary:")
    print(f"  Successfully updated: {success_count}")
    print(f"  Failed: {fail_count}")
    print(f"  Skipped (reference): 1")


if __name__ == "__main__":
    main()
