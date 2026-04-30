"""JSON storage layer for project data."""
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote


logger = logging.getLogger(__name__)


class JsonStore:
    """Handles loading and saving projects to a JSON file."""
    
    def __init__(self, filepath: str = None):
        if filepath is None:
            app_dir = self._get_app_dir()
            app_dir.mkdir(parents=True, exist_ok=True)
            self.filepath = app_dir / "projects.json"
            logger = logging.getLogger(__name__)
            logger.info(f"JsonStore using app directory: {app_dir}")
            logger.debug(f"Projects file: {self.filepath}")
        else:
            self.filepath = Path(filepath)
            logger.debug(f"JsonStore using custom filepath: {self.filepath}")
        if not self.filepath.exists():
            self.filepath.write_text('{"projects": []}')
            logger.debug(f"Created new projects file: {self.filepath}")
    
    def _get_app_dir(self) -> Path:
        """Get application data directory for storing projects."""
        if sys.platform == 'win32':
            base_dir = Path(os.environ.get('LOCALAPPDATA', Path.home()))
        else:
            base_dir = Path.home()
        app_dir = base_dir / '.viewpro'
        logger.debug(f"Application directory: {app_dir}")
        return app_dir
    
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
        logger.debug(f"add_project: {project.get('title', 'Unknown')} (logo: {project.get('logo')})")
        data = self.load()
        data['projects'].append(project)
        self.save(data)
    
    def remove_project(self, index: int) -> None:
        """Remove a project by index."""
        data = self.load()
        if 0 <= index < len(data['projects']):
            del data['projects'][index]
            self.save(data)
    
    def copy_logo(self, logo_path: str) -> Path:
        """Copy logo file to app directory with safe filename.
        
        Returns the relative path to the copied file.
        """
        if not logo_path:
            logger.debug("copy_logo: no logo path provided")
            return None
        
        src = Path(logo_path)
        logger.debug(f"copy_logo: source path: {src}")
        
        if not src.exists():
            logger.warning(f"copy_logo: source file not found: {src}")
            return None
        
        safe_name = self._make_safe_filename(src.name)
        dest_dir = self._get_app_dir() / "logos"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe_name
        
        logger.debug(f"copy_logo: destination: {dest}")
        
        if dest.exists():
            logger.info(f"copy_logo: logo already exists at {dest}")
            return dest.relative_to(self._get_app_dir())
        
        import shutil
        shutil.copy2(src, dest)
        logger.info(f"copy_logo: copied logo to {dest}")
        return dest.relative_to(self._get_app_dir())
    
    def _make_safe_filename(self, filename: str) -> str:
        """Create a safe filename from any input."""
        name = Path(filename).stem
        ext = Path(filename).suffix.lower()
        
        safe_name = re.sub(r'[^\w\s-]', '', name)
        safe_name = re.sub(r'[-\s]+', '_', safe_name)
        safe_name = safe_name.strip('_')
        
        if not safe_name:
            safe_name = 'logo'
        
        return f"{safe_name}{ext}"
