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

def fetch_models(provider_name: str, base_url: str, api_key: str, token_cache: dict | None = None) -> list[dict]:
    """Fetch models from a provider's /v1/models endpoint.
    
    Args:
        provider_name: Name of the provider
        base_url: Base URL for the API
        api_key: API key for authentication
        token_cache: Optional cache of token counts from other providers (model_id -> {input, output})
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.get(f"{base_url}/models", headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    
    models = []
    for model in data.get("data", []):
        model_id = model.get("id", "")
        # Determine capabilities from model id or defaults
        vision = "vision" in model_id.lower() or "vl" in model_id.lower()
        
        # Use max_model_len from API response (varies by model)
        # Split into input/output tokens (typically 90/10 or 95/5 split)
        max_model_len = model.get("max_model_len")
        
        if max_model_len:
            # API provides max_model_len - use it
            max_input_tokens = int(max_model_len * 0.9)
            max_output_tokens = int(max_model_len * 0.1)
        elif token_cache:
            # Try to find matching model in cache (case-insensitive, normalized)
            normalized_id = re.sub(r"^\d+\s*-\s*", "", model_id).lower()
            cached = None
            
            # Try exact match first, then normalized match
            if model_id in token_cache:
                cached = token_cache[model_id]
            elif normalized_id in token_cache:
                cached = token_cache[normalized_id]
            
            if cached:
                max_input_tokens = cached["maxInputTokens"]
                max_output_tokens = cached["maxOutputTokens"]
                print(f"    Using cached token counts for {model_id} (matched: {normalized_id})")
            else:
                # Fallback for models without max_model_len and no cache
                max_input_tokens = 128000
                max_output_tokens = 16000
        else:
            # Fallback for models without max_model_len and no cache
            max_input_tokens = 128000
            max_output_tokens = 16000
        
        # Determine tool calling mode based on model characteristics
        # Some models don't support "auto" mode properly
        
        model_config = {
            "id": model_id,
            "name": model_id,
            "url": base_url,
            "toolCalling": True,
            "vision": vision,
            "maxInputTokens": max_input_tokens,
            "maxOutputTokens": max_output_tokens
        }
        
        models.append(model_config)
    
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
    
    # Build a token cache from blablador first (it has proper max_model_len)
    token_cache = {}
    if "blablador" in providers:
        blablador_config = providers["blablador"].get("options", {})
        blablador_base_url = blablador_config.get("baseURL", "")
        blablador_api_key = blablador_config.get("apiKey", "")
        if blablador_base_url and blablador_api_key:
            print("Building token cache from blablador...")
            blablador_models = fetch_models("blablador", blablador_base_url, blablador_api_key)
            for model in blablador_models:
                model_id = model["id"]
                # Store by both original ID and normalized (lowercase, stripped) ID
                token_cache[model_id] = {
                    "maxInputTokens": model["maxInputTokens"],
                    "maxOutputTokens": model["maxOutputTokens"]
                }
                # Also store with normalized key (lowercase, strip prefix like "98 - ")
                normalized_id = re.sub(r"^\d+\s*-\s*", "", model_id).lower()
                if normalized_id != model_id.lower():
                    token_cache[normalized_id] = {
                        "maxInputTokens": model["maxInputTokens"],
                        "maxOutputTokens": model["maxOutputTokens"]
                    }
            print(f"  Cached token counts for {len(token_cache)} normalized model IDs")
    
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
        
        # Pass token_cache for providers other than blablador
        cache_to_use = None if provider_name.lower() == "blablador" else token_cache
        
        print(f"Fetching models from {provider_name}...")
        try:
            models = fetch_models(provider_name, base_url, api_key, cache_to_use)
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