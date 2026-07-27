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
from typing import Optional


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

def extract_context_length_via_llm(full_model_name: str, readme_content: str) -> Optional[int]:
    """Use alias-fast LLM to extract context length from README.
    
    Args:
        full_model_name: Full model name (e.g., "Qwen/qwen3-30b-a3b-instruct-2507")
        readme_content: Raw README markdown content
        
    Returns:
        Context length in tokens or None if not found
    """
    try:
        # Truncate README to first 8000 chars to stay within token limits
        context = readme_content[:8000]
        
        payload = {
            "model": "alias-fast",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a helpful assistant that only responds with a single integer representing the context length in tokens. Extract the context length from the model documentation. If you find 'Context Length', 'context_length', 'max_position_embeddings', or similar, return just the number. If not found, return 0. Do not include any explanation or text."
                },
                {
                    "role": "user",
                    "content": f"Extract the context length in tokens from this model README for {full_model_name}:\n\n{context}"
                }
            ],
            "temperature": 0.1
        }
        
        response = requests.post(
            "https://api.blablador.fz-juelich.de/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('BLABLADOR_TOKEN', '')}"},
            json=payload,
            timeout=30
        )
        
        if response.status_code != 200:
            return None
            
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        
        # Extract first number from response
        numbers = re.findall(r'\d+', content)
        if numbers:
            return int(numbers[0])
        return None
    except Exception as e:
        print(f"      LLM extraction failed: {e}")
        return None


def fetch_huggingface_model_info(model_name: str) -> Optional[dict]:
    """Fetch model metadata from HuggingFace Hub API.
    
    Args:
        model_name: Model name in format "organization/model-name" or "model-name"
        
    Returns:
        Dict with model info (vision, max tokens, etc.) or None if not found
    """
    try:
        full_model_name = None
        
        # Ensure model name has organization prefix using simple replacement rules
        if "/" not in model_name:
            model_lower = model_name.lower()
            
            # Apply replacement rules based on model name patterns
            if model_lower.startswith("qwen"):
                full_model_name = "Qwen/" + model_name
            elif model_lower.startswith("openai-"):
                # Strip "openai-" prefix and add "openai/"
                full_model_name = "openai/" + model_name[7:]  # 7 = len("openai-")
            elif model_lower.startswith("meta-llama") or model_lower.startswith("llama"):
                full_model_name = "meta-llama/" + model_name
            elif model_lower.startswith("mistral"):
                full_model_name = "mistralai/" + model_name
            elif model_lower.startswith("gemma"):
                full_model_name = "google/" + model_name
            elif model_lower.startswith("deepseek"):
                full_model_name = "deepseek-ai/" + model_name
            elif model_lower.startswith("glm"):
                full_model_name = "THUDM/" + model_name  # GLM models are from THUDM
            elif model_lower.startswith("devstral"):
                full_model_name = "microsoft/" + model_name
            elif model_lower.startswith("medgemma"):
                full_model_name = "google/" + model_name
            elif model_lower.startswith("apertus"):
                # Apertus models - try to find the base model
                full_model_name = "apertus/" + model_name
            elif model_lower.startswith("teuken"):
                full_model_name = "openGPT-X/" + model_name
            
            # Try to fetch the model
            if full_model_name:
                url = f"https://huggingface.co/api/models/{full_model_name}"
                response = requests.get(url, timeout=10)
                if response.status_code != 200:
                    full_model_name = None
        else:
            full_model_name = model_name
        
        if not full_model_name:
            return None
            
        # Fetch model info
        url = f"https://huggingface.co/api/models/{full_model_name}"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None
            
        data = response.json()
        
        # Debug: print raw response for analysis
        print(f"      HF pipeline_tag: {data.get('pipeline_tag')}")
        print(f"      HF config keys: {list(data.get('config', {}).keys()) if data.get('config') else 'None'}")
        
        # Extract pipeline_tag for capabilities
        pipeline_tag = data.get("pipeline_tag", "")
        tags = data.get("tags", [])
        
        # Check for vision capabilities
        vision = False
        if "vision" in full_model_name.lower() or "vl" in full_model_name.lower() or "omni" in full_model_name.lower():
            vision = True
        elif pipeline_tag in ["image-to-text", "visual-question-answering", "image-text-to-text", "any-to-any"]:
            vision = True
        elif any("vision" in tag.lower() for tag in tags):
            vision = True
            
        # Extract max tokens from config if available
        max_tokens = None
        config = data.get("config", {})
        if config:
            # Common max position embeddings field
            max_tokens = config.get("max_position_embeddings")
            if not max_tokens:
                # Try context_length or similar
                max_tokens = config.get("context_length")
            if not max_tokens:
                # Try max_seq_len or similar
                max_tokens = config.get("max_seq_len")
        
        # If no max tokens from config, try LLM extraction from README
        if not max_tokens:
            print(f"      Config has no max tokens, fetching README...")
            try:
                readme_url = f"https://huggingface.co/{full_model_name}/raw/main/README.md"
                readme_response = requests.get(readme_url, timeout=15)
                if readme_response.status_code == 200:
                    readme_content = readme_response.text
                    extracted = extract_context_length_via_llm(full_model_name, readme_content)
                    if extracted and extracted > 0:
                        max_tokens = extracted
                        print(f"      LLM extracted context length: {max_tokens}")
            except Exception as e:
                print(f"      README extraction failed: {e}")
        
        # Debug: print extracted values
        if vision or max_tokens:
            print(f"      Extracted: vision={vision}, max_tokens={max_tokens}")
        
        return {
            "vision": vision,
            "max_tokens": max_tokens,
            "full_name": full_model_name
        }
    except Exception as e:
        print(f"    Warning: Could not fetch HuggingFace info for {model_name}: {e}")
        return None


def fetch_models(provider_name: str, base_url: str, api_key: str, token_cache: dict | None = None, fetch_hf_metadata: bool = False) -> list[dict]:
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
        max_input_tokens = 128000
        max_output_tokens = 16000
        
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
        
        # Fetch HuggingFace metadata if enabled and we lack complete info
        hf_info = None
        if fetch_hf_metadata:
            # Always fetch HF metadata for GWDG to get proper model names and capabilities
            print(f"    Fetching HuggingFace metadata for {model_id}...")
            hf_info = fetch_huggingface_model_info(model_id)
            if hf_info:
                print(f"    [OK] Found: {hf_info['full_name']}")
                # HF vision info takes precedence
                if hf_info["vision"]:
                    vision = hf_info["vision"]
                    print(f"      Vision: enabled")
                # HF max_tokens ALWAYS takes precedence for GWDG (temporary workaround)
                if hf_info["max_tokens"]:
                    max_input_tokens = int(hf_info["max_tokens"] * 0.9)
                    max_output_tokens = int(hf_info["max_tokens"] * 0.1)
                    print(f"      Max tokens: {hf_info['max_tokens']} (input: {max_input_tokens}, output: {max_output_tokens})")
            else:
                print(f"    [FAIL] No match found on HuggingFace")
        
        # Determine tool calling mode based on model characteristics
        # Some models don't support "auto" mode properly
        
        model_config = {
            "id": model_id,
            "name": hf_info["full_name"] if hf_info and "/" in hf_info.get("full_name", "") else model_id,
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
    # Determine paths - use platform-specific VS Code settings location
    home = Path(os.path.expanduser("~"))
    opencode_path = home / ".config" / "opencode" / "opencode.json"
    
    # VS Code settings path differs by platform
    if os.name == "nt":  # Windows
        output_path = home / "AppData" / "Roaming" / "Code" / "User" / "chatLanguageModels.json"
    else:  # Linux/macOS
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
        
        # Enable HuggingFace metadata fetching for GWDG/SAIA
        fetch_hf = provider_name.lower() in ["gwdg", "saia"]
        
        print(f"Fetching models from {provider_name}...")
        try:
            models = fetch_models(provider_name, base_url, api_key, cache_to_use, fetch_hf_metadata=fetch_hf)
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