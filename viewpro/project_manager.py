"""Project management logic."""
from storage.json_store import JsonStore


class ProjectManager:
    """Manages project data operations."""
    
    def __init__(self, store: JsonStore = None):
        self.store = store or JsonStore()
    
    def get_projects(self) -> list:
        """Get all projects."""
        return self.store.get_projects()
    
    def get_project_path(self, project: dict) -> str:
        """Get path from project dict."""
        return project.get('path', '')
    
    def add_project(self, path: str, title: str, description: str, logo: str = None) -> bool:
        """Add a new project.
        
        Returns True if successful, False otherwise.
        """
        if not path or not title:
            return False
        
        path = path.strip()
        title = title.strip()
        description = description.strip()
        if logo:
            logo = logo.strip()
        
        if not path or not title:
            return False
        
        project = {
            'path': path,
            'title': title,
            'description': description,
            'logo': logo
        }
        
        self.store.add_project(project)
        return True
    
    def remove_project(self, index: int) -> bool:
        """Remove a project by index."""
        try:
            self.store.remove_project(index)
            return True
        except (IndexError, ValueError):
            return False
