#!/usr/bin/env python3
"""
Set Visual Studio Code as default for appropriate text-based file types.

This script manages desktop file defaults by updating mimeinfo.cache or
using xdg-mime to set defaults for appropriate text-based file types.
"""

import os
import sys
import subprocess
from pathlib import Path

# MIME types that VS Code should handle (text-based, code-related)
# Format: (mime_type, description)
VS_CODE_MIME_TYPES = [
    # Configuration files
    ("application/json", "JSON configuration files"),
    ("application/xml", "XML files"),
    ("application/x-yaml", "YAML files"),
    ("application/yaml", "YAML files"),
    # Source code files
    ("text/x-c", "C source files"),
    ("text/x-c++", "C++ source files"),
    ("text/x-c++hdr", "C++ header files"),
    ("text/x-c++src", "C++ source files"),
    ("text/x-chdr", "C header files"),
    ("text/x-csrc", "C source files"),
    ("text/x-java", "Java source files"),
    ("text/x-pascal", "Pascal source files"),
    ("text/x-tcl", "Tcl source files"),
    ("text/x-tex", "TeX source files"),
    ("text/x-xsrc", "X source files"),
    # Scripting languages
    ("text/x-python", "Python source files"),
    ("text/x-perl", "Perl source files"),
    ("text/x-php", "PHP source files"),
    ("text/x-ruby", "Ruby source files"),
    ("text/x-sh", "Shell scripts"),
    ("text/x-makefile", "Makefiles"),
    # Web development
    ("text/html", "HTML files"),
    ("text/css", "CSS files"),
    ("text/javascript", "JavaScript files"),
    ("application/javascript", "JavaScript files"),
    ("application/x-javascript", "JavaScript files"),
    ("text/xml", "XML files"),
    ("application/json", "JSON files"),
    ("application/xml", "XML files"),
    ("application/x-httpd-php", "PHP files"),
    # Data formats
    ("text/csv", "CSV files"),
    ("text/tab-separated-values", "Tab-separated values"),
    ("text/x-comma-separated-values", "CSV files"),
    ("text/x-csv", "CSV files"),
    # Markup and documentation
    ("text/markdown", "Markdown files"),
    ("text/plain", "Plain text files"),
    ("text/x-gettext-translation", "Translation files"),
    ("text/x-gettext-translation-template", "Translation templates"),
    # Version control
    ("text/x-patch", "Patch files"),
    # Configuration and custom formats
    ("text/x-ldif", "LDIF files"),
    ("text/x-vcard", "vCard files"),
    ("text/x-xmi", "XMI files"),
]

# Default VS Code desktop file
VS_CODE_DESKTOP = "code.desktop"


def check_desktop_file():
    """Check if VS Code desktop file exists."""
    desktop_paths = [
        Path("/usr/share/applications/code.desktop"),
        Path("/usr/local/share/applications/code.desktop"),
        Path.home() / ".local/share/applications/code.desktop",
    ]

    for path in desktop_paths:
        if path.exists():
            print(f"Found VS Code desktop file: {path}")
            return True

    print("Error: VS Code desktop file not found")
    return False


def get_current_default(mime_type):
    """Get current default application for a MIME type."""
    try:
        result = subprocess.run(
            ["xdg-mime", "query", "default", mime_type],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not query default for {mime_type}: {e}")
        return None


def set_default(mime_type, desktop_file):
    """Set default application for a MIME type."""
    try:
        subprocess.run(
            ["xdg-mime", "default", desktop_file, mime_type],
            check=True,
            capture_output=True,
        )
        print(f"✓ Set {desktop_file} as default for {mime_type}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ Failed to set {mime_type}: {e}")
        return False


def update_mimeinfo_cache():
    """Update the MIME info cache after changes."""
    try:
        subprocess.run(
            ["update-desktop-database", "-q", "/usr/share/applications"],
            check=True,
            capture_output=True,
        )
        print("Updated MIME info cache")
    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not update MIME cache: {e}")


def backup_mimeinfo():
    """Create a backup of mimeinfo.cache."""
    mimeinfo_path = Path("/usr/share/applications/mimeinfo.cache")
    backup_path = mimeinfo_path.with_suffix(".cache.bak")

    if mimeinfo_path.exists():
        try:
            import shutil

            shutil.copy2(mimeinfo_path, backup_path)
            print(f"Backed up mimeinfo.cache to {backup_path}")
            return True
        except Exception as e:
            print(f"Warning: Could not create backup: {e}")
            return False
    return False


def read_mimeinfo_cache():
    """Read the current mimeinfo.cache file."""
    mimeinfo_path = Path("/usr/share/applications/mimeinfo.cache")

    if not mimeinfo_path.exists():
        return {}

    mime_dict = {}
    with open(mimeinfo_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("["):
                continue

            if "=" in line:
                mime_type, apps = line.split("=", 1)
                apps = apps.strip(";").split(";")
                mime_dict[mime_type.strip()] = [a.strip() for a in apps if a.strip()]

    return mime_dict


def write_mimeinfo_cache(mime_dict):
    """Write the mimeinfo.cache file."""
    mimeinfo_path = Path("/usr/share/applications/mimeinfo.cache")

    with open(mimeinfo_path, "w") as f:
        f.write("[MIME Cache]\n")
        for mime_type, apps in sorted(mime_dict.items()):
            if apps:
                f.write(f"{mime_type}={';'.join(apps)};\n")


def add_vs_code_to_cache(mime_dict):
    """Add VS Code as first option for appropriate MIME types in cache."""
    desktop_file = VS_CODE_DESKTOP

    for mime_type, description in VS_CODE_MIME_TYPES:
        if mime_type not in mime_dict:
            mime_dict[mime_type] = [desktop_file]
            print(f"Added {mime_type} -> {desktop_file}")
        else:
            apps = mime_dict[mime_type]
            if desktop_file not in apps:
                apps.insert(0, desktop_file)
                mime_dict[mime_type] = apps
                print(f"Updated {mime_type}: {desktop_file} now first")

    return mime_dict


def main():
    """Main function."""
    print("=" * 60)
    print("VS Code Default Application Setup")
    print("=" * 60)
    print()

    if not check_desktop_file():
        sys.exit(1)

    # Check if we have permission
    mimeinfo_path = Path("/usr/share/applications/mimeinfo.cache")
    if mimeinfo_path.exists() and not os.access(mimeinfo_path, os.W_OK):
        print("Warning: Need root access to modify /usr/share/applications/")
        print("Consider using: sudo python3 set_vscode_defaults.py")
        print()

    # Backup existing configuration
    backup_mimeinfo()
    print()

    # Option to use xdg-mime (user-level) or modify mimeinfo.cache (system-level)
    use_xdg = True

    if use_xdg:
        print("Using xdg-mime to set defaults (user-level)...")
        print()

        success_count = 0
        fail_count = 0

        for mime_type, description in VS_CODE_MIME_TYPES:
            current = get_current_default(mime_type)
            if current != VS_CODE_DESKTOP:
                if set_default(mime_type, VS_CODE_DESKTOP):
                    success_count += 1
                else:
                    fail_count += 1
            else:
                print(f"  {mime_type}: already set to {VS_CODE_DESKTOP}")

        print()
        print(f"Summary: {success_count} updated, {fail_count} failed")
        update_mimeinfo_cache()
    else:
        print("Modifying mimeinfo.cache (system-level)...")
        print()

        mime_dict = read_mimeinfo_cache()
        mime_dict = add_vs_code_to_cache(mime_dict)
        write_mimeinfo_cache(mime_dict)
        update_mimeinfo_cache()

    print()
    print("Done! VS Code is now the default for text-based and code files.")


if __name__ == "__main__":
    main()
