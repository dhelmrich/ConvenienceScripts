#!/usr/bin/env python3
"""
Sync language models from opencode.json providers to VS Code chatLanguageModels.json
"""

import json
import os
import re
import sys
import requests
from pathlib import Path

def get_opencode_config() -> dict:
    """Load opencode.json configuration, stripping JavaScript-style comments."""
    config_path = Path(os.path.expanduser("~/.config/opencode/opencode.json"))
    with open(config_path, "r") as f:
        content = f.read()
    # Remove // comments (but not // in URLs)
    content = re.sub(r"^\s*//.*$", "", content, flags=re.MULTILINE)
    # Remove trailing commas before ] or }
    content = re.sub(r",(\s*[\]}])", r"\1", content)
    return json.loads(content)

def fetch_models(provider_name: str, base_url: str, api_key: str) -> list[dict]:
    """Fetch models from a provider's /v1/models endpoint."""
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.get(f"{base_url}/models", headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    
    models = []
    for model in data.get("data", []):
        model_id = model.get("id", "")
        # Determine capabilities from model id or defaults
        vision = "vision" in model_id.lower() or "vl" in model_id.lower()
        
        models.append({
            "id": model_id,
            "name": model_id,
            "url": base_url,
            "toolCalling": True,
            "vision": vision,
            "maxInputTokens": model.get("max_tokens", 128000),
            "maxOutputTokens": 16000
        })
    
    return models

def load_existing_config(output_path: Path) -> list[dict]:
    """Load existing chatLanguageModels.json to preserve apiKey secrets."""
    if output_path.exists():
        with open(output_path, "r") as f:
            return json.load(f)
    return []

def find_existing_provider(existing: list[dict], target_name: str) -> dict | None:
    """Find existing provider by name, handling SAIA -> GWDG mapping."""
    name_map = {"saia": ["gwdg", "saia"], "blablador": ["blablador"]}
    search_names = name_map.get(target_name.lower(), [target_name.lower()])
    
    for provider in existing:
        if provider.get("name", "").lower() in search_names:
            return provider
    return None

def main():
    # Determine paths relative to $HOME
    home = Path(os.path.expanduser("~"))
    opencode_path = home / ".config" / "opencode" / "opencode.json"
    output_path = home / ".config" / "Code" / "User" / "chatLanguageModels.json"
    
    # Load opencode config and existing chatLanguageModels
    with open(opencode_path, "r") as f:
        content = f.read()
    content = re.sub(r"^\s*//.*$", "", content, flags=re.MULTILINE)
    content = re.sub(r",(\s*[\]}])", r"\1", content)
    config = json.loads(content)
    
    existing = load_existing_config(output_path)
    providers = config.get("provider", {})
    
    # Build new config, preserving apiKey from existing entries
    new_providers = []
    for provider_name, provider_config in providers.items():
        options = provider_config.get("options", {})
        base_url = options.get("baseURL", "")
        api_key = options.get("apiKey", "")
        
        if not base_url or not api_key:
            print(f"Skipping {provider_name}: missing baseURL or apiKey")
            continue
        
        # Find existing entry to preserve apiKey secret reference
        existing_entry = find_existing_provider(existing, provider_name)
        preserved_api_key = existing_entry.get("apiKey", api_key) if existing_entry else api_key
        
        print(f"Fetching models from {provider_name}...")
        try:
            models = fetch_models(provider_name, base_url, api_key)
            new_providers.append({
                "name": provider_name.capitalize(),
                "vendor": "customendpoint",
                "apiKey": preserved_api_key,
                "apiType": "chat-completions",
                "apiEndpoint": base_url,
                "models": models
            })
            print(f"  Found {len(models)} models")
        except Exception as e:
            print(f"  Error: {e}")
    
    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, "w") as f:
        json.dump(new_providers, f, indent=2)
    
    print(f"\nWritten {sum(len(p['models']) for p in new_providers)} models to {output_path}")

if __name__ == "__main__":
    main()