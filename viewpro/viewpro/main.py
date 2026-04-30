"""Main entry point with CLI support."""
import sys
import os
import json
import argparse
import logging
from pathlib import Path
from .qt_compat import QApplication

from .ui.main_window import MainWindow
from .storage.json_store import JsonStore
from .project_manager import ProjectManager


def add_project_cli(path: str, title: str = None, description: str = None, logo: str = None):
    """Add a project via CLI."""
    
    store = JsonStore()
    manager = ProjectManager(store)
    
    if not path:
        print("Error: Project path is required")
        return False
    
    if not title:
        title = input("Enter project title: ").strip()
    
    if not description:
        description = input("Enter project description: ").strip()
    
    if not logo:
        logo = input("Enter logo path (optional, press Enter to skip): ").strip()
        if not logo:
            logo = None
    
    if manager.add_project(path, title, description, logo):
        print(f"Project '{title}' added successfully!")
        return True
    else:
        print("Error: Failed to add project")
        return False


def import_projects_cli(source_path: str):
    """Import projects from an external JSON file."""
    logger = logging.getLogger(__name__)
    logger.info(f"import_projects_cli: source path: {source_path}")
    
    source = Path(source_path)
    if not source.exists():
        logger.error(f"import_projects_cli: source file not found: {source_path}")
        print(f"Error: Source file not found: {source_path}")
        return False
    
    try:
        with open(source, 'r') as f:
            data = json.load(f)
        logger.info(f"import_projects_cli: loaded JSON from {source_path}")
    except json.JSONDecodeError as e:
        logger.error(f"import_projects_cli: invalid JSON in source file: {e}")
        print(f"Error: Invalid JSON in source file: {e}")
        return False
    
    store = JsonStore()
    app_dir = store._get_app_dir()
    logger.info(f"import_projects_cli: app directory: {app_dir}")
    
    current_data = store.load()
    imported_count = 0
    
    if 'projects' in data:
        manager = ProjectManager(store)
        for project in data['projects']:
            path = project.get('path', '')
            title = project.get('title', '')
            description = project.get('description', '')
            logo = project.get('logo')
            
            logger.debug(f"import_projects_cli: processing project '{title}', logo: {logo}")
            
            if path and title:
                manager.add_project(path, title, description, logo)
                imported_count += 1
    
    store.save(current_data)
    logger.info(f"import_projects_cli: successfully imported {imported_count} project(s)")
    print(f"Successfully imported {imported_count} project(s)")
    return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="ViewPro - Project Manager")
    parser.add_argument('--add', '-a', metavar='PATH', help='Add project by path (CLI mode)')
    parser.add_argument('--title', '-t', metavar='TITLE', help='Project title (use with --add)')
    parser.add_argument('--description', '-d', metavar='DESC', help='Project description (use with --add)')
    parser.add_argument('--logo', '-l', metavar='LOGO', help='Logo image path (use with --add)')
    parser.add_argument('--import', '-i', dest='import_path', metavar='PATH', help='Import projects from JSON file')
    
    args = parser.parse_args()
    
    if args.import_path:
        import_projects_cli(args.import_path)
    elif args.add:
        add_project_cli(args.add, args.title, args.description, args.logo)
    else:
        import sys
        if hasattr(sys, 'frozen'):
            script_dir = Path(sys.executable).parent
        else:
            script_dir = Path(__file__).resolve().parent
        
        store = JsonStore()
        app_dir = store._get_app_dir()
        
        log_dir = app_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        debug_log = log_dir / "debug.log"
        error_log = log_dir / "error.log"
        
        logging.basicConfig(
            filename=debug_log,
            level=logging.DEBUG,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        logger = logging.getLogger(__name__)
        logger.info("=" * 60)
        logger.info("=== New Application Instance Started ===")
        logger.info("=" * 60)
        logger.info(f"Script directory: {script_dir}")
        logger.info(f"App directory: {app_dir}")
        logger.info(f"Debug log: {debug_log}")
        logger.info(f"Error log: {error_log}")
        
        sys.stdout = open(debug_log, 'a')
        sys.stderr = open(error_log, 'a')
        
        app = QApplication(sys.argv)
        logger.info("QApplication created")
        
        store = JsonStore()
        logger.info("JsonStore created")
        
        manager = ProjectManager(store)
        logger.info("ProjectManager created")
        
        window = MainWindow(manager)
        logger.info("MainWindow created, showing window")
        window.show()
        logger.info("Window shown, starting event loop")
        
        try:
            result = app.exec()
            logger.info("=== Application exited normally ===")
            sys.exit(result)
        except Exception as e:
            logger.exception("Exception in event loop")
            sys.exit(1)


if __name__ == '__main__':
    main()
