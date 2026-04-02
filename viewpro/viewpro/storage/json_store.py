"""JSON storage layer for project data."""
import json
import os
from pathlib import Path


class JsonStore:
    """Handles loading and saving projects to a JSON file."""
    
    def __init__(self, filepath: str = None):
        if filepath is None:
            self.filepath = Path(__file__).resolve().parent.parent / "projects.json"
        else:
            self.filepath = Path(filepath)
        if not self.filepath.exists():
            self.filepath.write_text('{"projects": []}')
    
    def load(self) -> dict:
        """Load projects from JSON file."""
        with open(self.filepath, 'r') as f:
            return json.load(f)
    
    def save(self, data: dict) -> None:
        """Save projects to JSON file."""
        with open(self.filepath, 'w') as f:
            json.dump(data, f, indent=2)
    
    def get_projects(self) -> list:
        """Get list of projects."""
        data = self.load()
        return data.get('projects', [])
    
    def update_last_clicked(self, project_path: str) -> None:
        """Update the last clicked timestamp for a project."""
        data = self.load()
        for project in data.get('projects', []):
            if project.get('path') == project_path:
                project['last_clicked'] = self._get_timestamp()
                break
        self.save(data)
    
    def _get_timestamp(self) -> str:
        """Get current timestamp in ISO format."""
        from datetime import datetime
        return datetime.now().isoformat()
    
    def add_project(self, project: dict) -> None:
        """Add a new project."""
        data = self.load()
        data['projects'].append(project)
        self.save(data)
    
    def remove_project(self, index: int) -> None:
        """Remove a project by index."""
        data = self.load()
        if 0 <= index < len(data['projects']):
            del data['projects'][index]
            self.save(data)
