"""Project management logic."""
import logging
from .storage.json_store import JsonStore

logger = logging.getLogger(__name__)


class ProjectManager:
    """Manages project data operations."""
    
    def __init__(self, store: JsonStore = None):
        self.store = store or JsonStore()
    
    def get_projects(self) -> list:
        """Get all projects sorted by last clicked time (newest first)."""
        projects = self.store.get_projects()
        projects.sort(key=lambda p: p.get('last_clicked', ''), reverse=True)
        return projects
    
    def get_project_path(self, project: dict) -> str:
        """Get path from project dict."""
        return project.get('path', '')
    
    def add_project(self, path: str, title: str, description: str, logo: str = None, start_script: str = None) -> bool:
        """Add a new project.
        
        Returns True if successful, False otherwise.
        """
        if not path or not title:
            logger.warning(f"add_project: missing path or title")
            return False
        
        path = path.strip()
        title = title.strip()
        description = description.strip()
        if logo:
            logo = logo.strip()
        if start_script:
            start_script = start_script.strip()
        
        if not path or not title:
            logger.warning(f"add_project: empty path or title after strip")
            return False
        
        logo_dest = None
        if logo:
            logo_dest = self.store.copy_logo(logo)
            logger.debug(f"add_project: logo copied to {logo_dest}")
        
        project = {
            'path': path,
            'title': title,
            'description': description,
            'logo': str(logo_dest) if logo_dest else None,
            'start_script': start_script
        }
        
        logger.info(f"add_project: adding project '{title}' with logo: {project['logo']}")
        self.store.add_project(project)
        return True
    
    def remove_project(self, index: int) -> bool:
        """Remove a project by index."""
        try:
            self.store.remove_project(index)
            return True
        except (IndexError, ValueError):
            return False
