#!/bin/bash
set -e

source_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/blacommand"

if [[ ! -f "$source_script" ]]; then
    echo "Error: Could not find source script at $source_script"
    exit 1
fi

target_dir="$HOME/.local/share/ConvenienceScripts"
target_script="$target_dir/blacommand"

mkdir -p "$target_dir"
cp -f "$source_script" "$target_script"
chmod +x "$target_script"

shell_rc=""
if [[ -f "$HOME/.bashrc" ]]; then
    shell_rc="$HOME/.bashrc"
elif [[ -f "$HOME/.zshrc" ]]; then
    shell_rc="$HOME/.zshrc"
fi

if [[ -n "$shell_rc" ]]; then
    path_entry="export PATH=\"\$PATH:$target_dir\""
    if ! grep -qF "$path_entry" "$shell_rc"; then
        echo "" >> "$shell_rc"
        echo "# Add blacommand to PATH" >> "$shell_rc"
        echo "$path_entry" >> "$shell_rc"
        echo "Added to $shell_rc: $target_dir"
    else
        echo "$shell_rc already contains PATH entry for: $target_dir"
    fi
else
    echo "Warning: Could not find shell RC file (.bashrc or .zshrc)"
    echo "Manually add to your PATH: $target_dir"
fi

if echo "$PATH" | grep -qF "$target_dir"; then
    echo "PATH already contains: $target_dir"
fi

dependencies_met=true
missing_deps=()

if command -v jq >/dev/null 2>&1; then
    echo "[OK] jq available"
else
    dependencies_met=false
    missing_deps+=("jq")
fi

if command -v curl >/dev/null 2>&1; then
    echo "[OK] curl available"
else
    dependencies_met=false
    missing_deps+=("curl")
fi

if [[ "$dependencies_met" == true ]]; then
    echo ""
    echo "Installed: $target_script"
    echo ""
    echo "Usage: blacommand your request"
    echo ""
    echo "Note: You may need to restart your shell or run 'source ~/.bashrc' (or 'source ~/.zshrc')"
    echo "      before 'blacommand' is available."
else
    echo ""
    echo "Warning: Missing dependencies. Please install:"
    for dep in "${missing_deps[@]}"; do
        echo "  - $dep: sudo apt install $dep"
    done
fi
