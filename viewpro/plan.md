# Project Plan: ViewPro - Project Viewer/Manager

## Overview
A PyQt application to view and manage developer projects with local JSON storage.

## Features
- Display projects in a 2-column grid/list layout
- Show: title, description, logo, backend path
- Radio button toggle: open in code editor or opencode
- Add new projects via GUI or CLI
- Local JSON storage for project data

## Project Structure
```
viewpro/
├── main.py              # Main application entry point
├── project_manager.py   # Project data management (load/save/add)
├── ui/
│   ├── __init__.py
│   └── main_window.py   # Main window UI and logic
├── storage/
│   ├── __init__.py
│   └── json_store.py    # JSON file operations
├── projects.json        # Default data file
└── plan.md             # This file
```

## Implementation Steps

### Step 1: Create Plan Document
- [x] Create plan.md (this file)

### Step 2: Setup Project Structure
- [x] Create directory structure
- [x] Create __init__.py files
- [x] Create basic projects.json with sample data

### Step 3: Implement Storage Layer
- [x] `storage/json_store.py` - Load/save projects to JSON
- [x] Handle file not found gracefully

### Step 4: Implement Project Manager
- [x] `project_manager.py` - Manage project data
- [x] Methods: add_project(), get_projects(), remove_project()
- [x] Validate required fields (path, title, description)

### Step 5: Implement UI
- [x] `ui/main_window.py` - Main window
- [x] 2-column layout with QGridLayout
- [x] Radio buttons for open action (code/opencode)
- [x] Add project button/dialog
- [x] Open project functionality

### Step 6: CLI Integration
- [x] Add argparse for CLI
- [x] `python main.py --add <path> --title <title> --description <desc>`
- [x] GUI mode by default when no args

### Step 7: Testing
- [x] Test GUI launch
- [x] Test CLI help works
- [ ] Test adding project via GUI
- [ ] Test adding project via CLI
- [ ] Test project opening (code/opencode)

### Step 8: Project Card Layout
- [x] Fixed 360x120px card size
- [x] Logo (100x100) on left
- [x] Title (11pt bold), description (10pt), path (9pt) on right
- [x] Clickable to open project
- [x] Radio button controls open mode (code/opencode)

## Data Format (projects.json)
```json
{
  "projects": [
    {
      "path": "/absolute/path/to/project",
      "title": "Project Name",
      "description": "Project description",
      "logo": null
    }
  ]
}
```

## Dependencies
- PyQt6 (or PyQt5 fallback)
- Python standard library only (argparse, json, os, pathlib)
