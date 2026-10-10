# Local CI/CD Pipeline

A lightweight, fully local Continuous Integration and Continuous Deployment (CI/CD) pipeline execution engine, inspired by GitHub Actions. 

This tool automatically detects new Git commits, clones the repository into an isolated workspace, resolves stage dependencies, and executes commands (including Docker builds) safely and sequentially.

---

## 🏗️ Architecture

The system is decoupled into three primary components:
1. **`config_parser.py`**: Reads `pipeline.yml`, validates the schema, and uses Kahn's algorithm for topological sorting to determine safe execution order and parallel stage groups.
2. **`runner.py`**: The execution engine. It manages subprocess execution, real-time log streaming, environment variable merging, failure handling, and state persistence.
3. **`watcher.py`**: A polling daemon that watches a target Git repository for new commits. It enforces strict isolation by cloning the commit into a clean `.cicd/workspaces/<sha>` directory before triggering the runner.

---

## ⚙️ Prerequisites & Installation

* **Python 3.8+**
* **Git** (The project directory must be a valid Git repository).
* **Docker** (The Docker daemon must be running to execute containerization stages).

**Installation Steps:**
1. Clone or download this project folder.
2. Initialize Git if not already done: `git init`.
3. Ensure your deployment scripts are executable: 
   ```bash
   chmod +x scripts/deploy.sh
