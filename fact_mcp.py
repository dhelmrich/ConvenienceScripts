"""
An MCP Server which will store and encode a knowledge database in a JSON file. This database can be prompted using singular words or phrases which are connected to a taxonomy if applicable. The server will respond using the phrases that are stored in the database. If a prompt is not found in the database, the server will respond with a default message. The server will also have an endpoint to add new facts to the database. The facts will be stored in a JSON file with the following structure:
{
    "facts": [
        {
            "id": "unique_id",
            "prompt": "the prompt that will trigger this fact",
            "response": "the response that will be returned when the prompt is triggered",
            "taxonomy": "the taxonomy that this fact belongs to (optional)"
            },
        ...
    ]
}

IMPORTANT USAGE GUIDELINES FOR AI ASSISTANTS:
- ALWAYS check the facts database first when you need information about people, users, projects, or domain-specific knowledge.
- When you encounter a task involving a person's name, role, or identity, call get_fact('name') or knowledge_about('User') to retrieve stored user information.
- When working on a project, call knowledge_about('<project_name>') to understand the project context before proceeding.
- The facts database contains pre-grouped knowledge by taxonomy for quick access to related information.
- If a prompt returns an ambiguity warning, use knowledge_about(taxonomy, prompt) to specify the context.

Key tools:
- get_fact(prompt): Quick lookup for specific prompts like 'name', 'email', 'role'
- knowledge_about(taxonomy, category): Browse all facts in a taxonomy, optionally filtered by category
- list_taxonomies(): Discover what knowledge domains are available
- add_fact(prompts, response, taxonomy): Add new knowledge (requires user approval)

Example usage patterns:
1. Before drafting an email to someone: get_fact('name') to know who you are
2. Before working on a project: knowledge_about('Synavis') to understand the project
3. When unsure about domain terminology: knowledge_about('<domain>') to find relevant facts

"""

import os
import sys
import json
import uuid
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(name="fact")


class FactStore:
    """Handles loading and saving facts to a JSON file."""
    
    def __init__(self, filepath: str = None):
        if filepath is None:
            app_dir = self._get_app_dir()
            app_dir.mkdir(parents=True, exist_ok=True)
            self.filepath = app_dir / "facts.json"
        else:
            self.filepath = Path(filepath)
        
        if not self.filepath.exists():
            self.filepath.write_text('{"facts": []}')
        
        self._facts = None
        self._load()
    
    def _get_app_dir(self) -> Path:
        """Get application data directory for storing facts."""
        if sys.platform == 'win32':
            base_dir = Path(os.environ.get('LOCALAPPDATA', Path.home()))
        else:
            base_dir = Path.home()
        app_dir = base_dir / '.fact_mcp'
        return app_dir
    
    def _load(self) -> None:
        """Load facts from JSON file and build indexes."""
        with open(self.filepath, 'r') as f:
            self._data = json.load(f)
        
        self._facts = self._data.get('facts', [])
        self._build_indexes()
    
    def _build_indexes(self) -> None:
        """Build indexes for quick lookup."""
        self._prompt_index = {}
        self._taxonomy_index = {}
        
        for fact in self._facts:
            fact_id = fact.get('id')
            taxonomy = fact.get('taxonomy')
            
            if taxonomy:
                if taxonomy not in self._taxonomy_index:
                    self._taxonomy_index[taxonomy] = []
                self._taxonomy_index[taxonomy].append(fact)
            
            prompts = fact.get('prompts', [])
            for prompt in prompts:
                normalized = self._normalize(prompt)
                if normalized not in self._prompt_index:
                    self._prompt_index[normalized] = []
                self._prompt_index[normalized].append(fact)
    
    def _normalize(self, text: str) -> str:
        """Normalize text for matching."""
        return text.lower().strip()
    
    def save(self) -> None:
        """Save facts to JSON file."""
        self._data['facts'] = self._facts
        with open(self.filepath, 'w') as f:
            json.dump(self._data, f, indent=2)
    
    def get_fact_by_prompt(self, prompt: str) -> dict | None:
        """Get fact by matching prompt."""
        normalized = self._normalize(prompt)
        return self._prompt_index.get(normalized)
    
    def get_facts_by_taxonomy(self, taxonomy: str) -> list:
        """Get all facts belonging to a taxonomy."""
        return self._taxonomy_index.get(taxonomy, [])
    
    def get_all_taxonomies(self) -> list:
        """Get list of all taxonomies."""
        return list(self._taxonomy_index.keys())
    
    def add_fact(self, prompts: list, response: str, taxonomy: str = None) -> dict:
        """Add a new fact to the store."""
        fact = {
            'id': str(uuid.uuid4()),
            'prompts': prompts if isinstance(prompts, list) else [prompts],
            'response': response,
            'taxonomy': taxonomy
        }
        self._facts.append(fact)
        self.save()
        self._build_indexes()
        return fact
    
    def import_facts(self, facts: list) -> int:
        """Import facts from external source. Returns count of imported facts."""
        for fact in facts:
            if 'id' not in fact:
                fact['id'] = str(uuid.uuid4())
            self._facts.append(fact)
        self.save()
        self._build_indexes()
        return len(facts)
    
    def export_facts(self) -> dict:
        """Export all facts."""
        return {'facts': self._facts.copy()}
    
    def get_all_facts(self) -> list:
        """Get all facts."""
        return self._facts.copy()
    
    def update_fact(self, fact_id: str, prompts: list = None, response: str = None, taxonomy: str = None, old_prompts: list = None, old_response: str = None, old_taxonomy: str = None) -> dict:
        """Update an existing fact with optimistic concurrency control. Returns updated fact or error."""
        for i, fact in enumerate(self._facts):
            if fact.get('id') == fact_id:
                if old_prompts is not None:
                    if fact.get('prompts') != old_prompts:
                        return {'error': f'Fact prompts changed. Expected: {old_prompts}, Current: {fact.get("prompts")}. Must match exactly (including whitespace, brackets, quotes). Call get_fact_by_id to retrieve current value.'}
                if old_response is not None:
                    if fact.get('response') != old_response:
                        return {'error': f'Fact response changed. Expected: {old_response}, Current: {fact.get("response")}. Must match exactly (including whitespace, punctuation, capitalization). Call get_fact_by_id to retrieve current value.'}
                if old_taxonomy is not None:
                    if fact.get('taxonomy') != old_taxonomy:
                        return {'error': f'Fact taxonomy changed. Expected: {old_taxonomy}, Current: {fact.get("taxonomy")}. Must match exactly. Call get_fact_by_id to retrieve current value.'}
                
                if prompts is not None:
                    self._facts[i]['prompts'] = prompts if isinstance(prompts, list) else [prompts]
                if response is not None:
                    self._facts[i]['response'] = response
                if taxonomy is not None:
                    self._facts[i]['taxonomy'] = taxonomy
                
                self.save()
                self._build_indexes()
                return self._facts[i]
        
        return {'error': f'Fact with id {fact_id} not found'}
    
    def delete_fact(self, fact_id: str, expected_response: str = None, expected_prompts: list = None) -> dict:
        """Delete a fact by ID with mandatory validation. Returns success status."""
        for i, fact in enumerate(self._facts):
            if fact.get('id') == fact_id:
                if expected_response is not None:
                    if fact.get('response') != expected_response:
                        return {'success': False, 'error': f'Fact response changed. Expected: {expected_response}, Current: {fact.get("response")}. Must match exactly (including whitespace, punctuation, capitalization). Call get_fact_by_id to retrieve current value.'}
                if expected_prompts is not None:
                    if fact.get('prompts') != expected_prompts:
                        return {'success': False, 'error': f'Fact prompts changed. Expected: {expected_prompts}, Current: {fact.get("prompts")}. Must match exactly (including whitespace, brackets, quotes). Call get_fact_by_id to retrieve current value.'}
                
                deleted_fact = self._facts.pop(i)
                self.save()
                self._build_indexes()
                return {'success': True, 'deleted_id': fact_id}
        
        return {'success': False, 'error': f'Fact with id {fact_id} not found'}
    
    def search_facts(self, keyword: str) -> list:
        """Search for facts containing a keyword in their response. Returns list of matches with metadata."""
        keyword_lower = keyword.lower()
        matches = []
        
        for fact in self._facts:
            response = fact.get('response', '')
            if keyword_lower in response.lower():
                matches.append({
                    'fact_id': fact.get('id'),
                    'prompts': fact.get('prompts', []),
                    'taxonomy': fact.get('taxonomy', 'Unknown')
                })
        
        return matches
    
    def get_fact_by_id(self, fact_id: str) -> dict | None:
        """Get a fact by its ID. Returns the full fact or None."""
        for fact in self._facts:
            if fact.get('id') == fact_id:
                return fact.copy()
        return None


# Global fact store instance
_fact_store = None


def get_fact_store() -> FactStore:
    """Get or create the global fact store instance."""
    global _fact_store
    if _fact_store is None:
        _fact_store = FactStore()
    return _fact_store


@mcp.tool()
def get_fact(prompt: str) -> str:
    """Retrieve a fact by its prompt. Returns the stored response if found, otherwise returns a default message.
    
    USE THIS TOOL WHEN:
    - You need to know the user's name, email, role, or identity before taking action
    - You need domain-specific knowledge about a topic
    - You see a keyword in a task that might be in the facts database (e.g., 'name', 'project', 'epsilon')
    
    Common prompts: 'name', 'email', 'role', 'project', variable names like 'epsilon'
    
    Args:
        prompt: The prompt to search for in the fact database.
    """
    store = get_fact_store()
    facts = store.get_fact_by_prompt(prompt)
    
    if facts:
        if len(facts) > 1:
            taxonomies = list(set(f.get('taxonomy', 'Unknown') for f in facts))
            return f"This prompt is ambiguous and matches {len(facts)} facts across {len(taxonomies)} taxonomy/taxonomies: {', '.join(taxonomies)}. Please use knowledge_about('{taxonomies[0]}', '{prompt}') to specify which taxonomy you want."
        return facts[0].get('response', 'No response stored.')
    
    return "I don't have information about that in my knowledge base."


@mcp.tool()
def knowledge_about(taxonomy: str, category: str = None) -> str:
    """Get information about a specific taxonomy. Optionally filter by category.
    
    USE THIS TOOL WHEN:
    - You need comprehensive information about a project, domain, or topic
    - You want to discover all facts related to a taxonomy (e.g., all Synavis project details)
    - You need to understand context before working on something
    
    Args:
        taxonomy: The taxonomy to query (e.g., "Synavis", "User", "Coding").
        category: Optional category to filter by (e.g., "Project", "Person", "Technical").
    """
    store = get_fact_store()
    facts = store.get_facts_by_taxonomy(taxonomy)
    
    if not facts:
        return f"I don't have information about the {taxonomy} taxonomy."
    
    if category:
        matching_facts = [f for f in facts if category.lower() in f.get('response', '').lower()]
        if matching_facts:
            responses = [f.get('response', '') for f in matching_facts]
            combined = ' '.join(responses)
            return f"{taxonomy} is: {combined}"
        else:
            return f"I don't have information about {taxonomy} related to {category}."
    
    keywords = []
    for fact in facts:
        prompts = fact.get('prompts', [])
        keywords.extend(prompts)
    
    return ', '.join(keywords)


@mcp.tool()
def list_taxonomies() -> list:
    """List all available taxonomies in the fact database."""
    store = get_fact_store()
    return store.get_all_taxonomies()


@mcp.tool()
def add_fact(prompts: list, response: str, taxonomy: str = None) -> dict:
    """Add a new fact to the database.
    
    Args:
        prompts: List of prompts that will trigger this fact.
        response: The response to return when any of the prompts are matched.
        taxonomy: Optional taxonomy to categorize this fact.
    """
    store = get_fact_store()
    fact = store.add_fact(prompts, response, taxonomy)
    return {
        'success': True,
        'fact_id': fact['id'],
        'prompts': fact['prompts'],
        'taxonomy': fact.get('taxonomy')
    }


@mcp.tool()
def import_facts(json_data: str) -> dict:
    """Import facts from a JSON string.
    
    Args:
        json_data: JSON string containing facts in the format:
                   {"facts": [{"prompts": [...], "response": "...", "taxonomy": "..."}]}
    """
    try:
        data = json.loads(json_data)
        facts = data.get('facts', [])
        store = get_fact_store()
        count = store.import_facts(facts)
        return {'success': True, 'imported_count': count}
    except json.JSONDecodeError as e:
        return {'success': False, 'error': f'Invalid JSON: {str(e)}'}


@mcp.tool()
def export_facts() -> str:
    """Export all facts as JSON string."""
    store = get_fact_store()
    data = store.export_facts()
    return json.dumps(data, indent=2)


@mcp.tool()
def list_all_facts() -> list:
    """List all facts in the database."""
    store = get_fact_store()
    return store.get_all_facts()


@mcp.tool()
def update_fact(fact_id: str, prompts: list = None, response: str = None, taxonomy: str = None, old_prompts: list = None, old_response: str = None, old_taxonomy: str = None) -> dict:
    """Update an existing fact in the database with optimistic concurrency control.
    
    IMPORTANT: The old_* parameters MUST match the current value EXACTLY (including whitespace, punctuation, capitalization, brackets).
    If validation fails, the error message will show both expected and current values. Call get_fact_by_id first to retrieve the exact current state.
    
    Args:
        fact_id: The ID of the fact to update.
        prompts: New list of prompts that will trigger this fact.
        response: New response text.
        taxonomy: New taxonomy to categorize this fact.
        old_prompts: Expected current prompts for validation (required when updating prompts). Must match exactly.
        old_response: Expected current response for validation (required when updating response). Must match exactly.
        old_taxonomy: Expected current taxonomy for validation (required when updating taxonomy). Must match exactly.
    """
    store = get_fact_store()
    return store.update_fact(fact_id, prompts, response, taxonomy, old_prompts, old_response, old_taxonomy)


@mcp.tool()
def delete_fact(fact_id: str, expected_response: str = None, expected_prompts: list = None) -> dict:
    """Delete a fact from the database with mandatory validation.
    
    IMPORTANT: The expected_* parameters MUST match the current value EXACTLY (including whitespace, punctuation, capitalization, brackets).
    If validation fails, the error message will show both expected and current values. Call get_fact_by_id first to retrieve the exact current state.
    
    Args:
        fact_id: The ID of the fact to delete.
        expected_response: Expected current response for validation (required when deleting by response). Must match exactly.
        expected_prompts: Expected current prompts for validation (required when deleting by prompts). Must match exactly.
    """
    store = get_fact_store()
    return store.delete_fact(fact_id, expected_response, expected_prompts)


@mcp.tool()
def search_for(keyword: str) -> list:
    """Search for facts containing a keyword in their response.
    
    Returns a list of matches with fact_id, prompts, and taxonomy - not the full response text.
    Use get_fact or knowledge_about after finding relevant facts.
    
    Args:
        keyword: The keyword to search for in fact responses.
    """
    store = get_fact_store()
    return store.search_facts(keyword)


@mcp.tool()
def get_fact_by_id(fact_id: str) -> dict:
    """Get a fact by its ID. Returns the full fact including prompts, response, and taxonomy.
    
    USE THIS TOOL WHEN:
    - You need to check the current state of a fact before updating or deleting it
    - You have a fact_id and need to see its complete details
    
    Args:
        fact_id: The ID of the fact to retrieve.
    """
    store = get_fact_store()
    fact = store.get_fact_by_id(fact_id)
    if fact:
        return fact
    return {'error': f'Fact with id {fact_id} not found'}


if __name__ == "__main__":
    mcp.run()
