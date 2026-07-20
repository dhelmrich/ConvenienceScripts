#!/usr/bin/env python3
"""
Script to find and replace non-ASCII characters in text files.
Recursively scans a folder, prompts for replacements, and maintains a mapping file.
"""

import os
import sys
import json
import argparse
import unicodedata
from pathlib import Path


# Common folders to ignore (starting with dot)
IGNORE_FOLDERS = {'.git', '.svn', '.hg', '.idea', '.vscode', '__pycache__', 
                  'node_modules', 'venv', '.venv', 'env', '.env', 'dist', 'build'}


def should_ignore_folder(folder_name: str) -> bool:
    """Check if a folder should be ignored."""
    return folder_name.startswith('.') or folder_name in IGNORE_FOLDERS


def is_text_file(file_path: Path) -> bool:
    """Check if a file is likely a text file by trying to read it."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            f.read(1024)  # Try reading first 1KB
        return True
    except (UnicodeDecodeError, IOError):
        return False


def find_non_ascii(text: str) -> list:
    """Find all non-ASCII characters in text, excluding combining marks and variation selectors."""
    def is_relevant_char(char: str) -> bool:
        code = ord(char)
        # Skip combining marks
        if unicodedata.combining(char):
            return False
        # Skip variation selectors (U+FE00-U+FE0F and U+E0100-U+E01EF)
        if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
            return False
        return True
    return [char for char in text if ord(char) > 127 and is_relevant_char(char)]


def load_replace_map(map_path: Path) -> dict:
    """Load the replacement map from JSON file."""
    if map_path.exists():
        try:
            with open(map_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_replace_map(map_path: Path, replace_map: dict) -> None:
    """Save the replacement map to JSON file."""
    with open(map_path, 'w', encoding='utf-8') as f:
        json.dump(replace_map, f, ensure_ascii=False, indent=2)


def process_file(file_path: Path, replace_map: dict, map_path: Path) -> tuple[bool, int]:
    """
    Process a single file, replacing non-ASCII characters.
    Returns (file_modified, replacements_made) tuple.
    """
    try:
        # Read file content
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find non-ASCII characters
        non_ascii_chars = find_non_ascii(content)
        
        if not non_ascii_chars:
            return False, 0
        
        # Process each unique non-ASCII character in sorted order (by Unicode code point)
        modified = False
        replacements = 0
        for char in sorted(set(non_ascii_chars), key=ord):
            if char in replace_map:
                # Use existing replacement (skip if ignored)
                replacement_value = replace_map[char]
                if replacement_value is not None and char in content:
                    count = content.count(char)
                    content = content.replace(char, replacement_value)
                    modified = True
                    replacements += count
            else:
                # Prompt for replacement
                print(f"\nEncountered new symbol in {file_path}: '{char}' (U+{ord(char):04X})")
                try:
                    # Use input() for interactive prompt with echo
                    replacement = input("Replace with: ").strip()
                except (KeyboardInterrupt, EOFError):
                    print("\nInterrupted!")
                    sys.exit(1)
                
                # Validate replacement: must be one or more non-whitespace characters
                if replacement:
                    if any(c.isspace() for c in replacement):
                        print(f"  Error: Replacement must contain only non-whitespace characters.")
                        print(f"  Got: '{replacement}'")
                        raise ValueError(f"Invalid replacement: '{replacement}'")
                
                if replacement:
                    replace_map[char] = replacement
                    count = content.count(char)
                    content = content.replace(char, replacement)
                    modified = True
                    replacements += count
                    # Save map immediately to persist this replacement
                    save_replace_map(map_path, replace_map)
                    print(f"  Replaced '{char}' with '{replacement}' (map updated)")
                else:
                    # Mark as ignored in the map so we don't prompt again
                    replace_map[char] = None
                    save_replace_map(map_path, replace_map)
                    print(f"  Skipping '{char}' (ignored - will not prompt again)")
        
        # Write back if modified
        if modified:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  → Modified: {file_path}")
        
        return modified, replacements
        
    except (UnicodeDecodeError, IOError) as e:
        print(f"  ⚠️  Could not process {file_path}: {e}")
        return False, 0


def recurse_folder(folder_path: Path, replace_map: dict, map_path: Path, dry_run: bool = False) -> tuple[int, int]:
    """
    Recursively process all files in a folder.
    Returns (files_modified, replacements_made) tuple.
    """
    modified_count = 0
    replacements_count = 0
    
    print(f"\nIterating folder: {folder_path}")
    
    try:
        for root, dirs, files in os.walk(folder_path):
            # Filter out ignored directories (modifying dirs in-place)
            dirs[:] = sorted([d for d in dirs if not should_ignore_folder(d)])
            # Sort files for deterministic order
            files = sorted(files)
            
            for file_name in files:
                file_path = Path(root) / file_name
                
                # Skip the map file itself
                if file_path == map_path:
                    continue
                
                # Only process text files
                if not is_text_file(file_path):
                    continue
                
                file_modified, file_replacements = process_file(file_path, replace_map, map_path)
                if file_modified:
                    modified_count += 1
                    replacements_count += file_replacements
    except KeyboardInterrupt:
        print("\n\nInterrupted by user - exiting.")
        sys.exit(1)
    
    return modified_count, replacements_count


def main():
    # Get the directory where this script is located
    script_dir = Path(__file__).parent.resolve()
    
    parser = argparse.ArgumentParser(
        description='Find and replace non-ASCII characters in text files.'
    )
    parser.add_argument(
        'folder',
        type=str,
        help='Folder to scan recursively'
    )
    parser.add_argument(
        '--map-file',
        type=str,
        default=script_dir / 'replace_unicode.json',
        help='JSON file to store character replacements (default: replace_unicode.json in script directory)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be replaced without making changes'
    )
    
    args = parser.parse_args()
    
    # Validate folder
    folder_path = Path(args.folder).resolve()
    if not folder_path.exists():
        print(f"Error: Folder '{folder_path}' does not exist")
        sys.exit(1)
    
    if not folder_path.is_dir():
        print(f"Error: '{folder_path}' is not a folder")
        sys.exit(1)
    
    # Load existing replacement map
    map_path = Path(args.map_file).resolve()
    replace_map = load_replace_map(map_path)
    
    if replace_map:
        print(f"Loaded {len(replace_map)} existing replacements from {map_path}")
    
    print(f"Target folder: {folder_path}")
    print(f"Map file: {map_path}")
    if args.dry_run:
        print("DRY RUN MODE - No changes will be made")
    
    # Process folder
    modified_count, replacements_count = recurse_folder(folder_path, replace_map, map_path, args.dry_run)
    
    # Summary
    print(f"\n{'='*50}")
    print(f"Summary:")
    print(f"  Files modified: {modified_count}")
    print(f"  Total replacements: {replacements_count}")
    print(f"  Map entries: {len(replace_map)}")
    print(f"  Map saved to: {map_path}")
    
    if replace_map:
        print(f"\nCurrent replacement map:")
        for char, replacement in replace_map.items():
            print(f"  '{char}' (U+{ord(char):04X}) → '{replacement}'")


if __name__ == '__main__':
    main()
