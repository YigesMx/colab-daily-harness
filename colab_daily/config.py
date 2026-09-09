"""Project-root anchored configuration; no network or legacy service dependency."""
from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import dotenv_values


@dataclass(frozen=True)
class Config:
    project_root: Path
    storage_dir: Path
    backup_dir: Path
    site_dir: Path
    working_dir: Path

    @classmethod
    def load(cls, project_root=None, environ=None):
        env = dict(os.environ if environ is None else environ)
        root = Path(project_root or env.get("PROJECT_ROOT") or Path(__file__).parents[1]).absolute()
        values = {**dotenv_values(root / ".env"), **env}

        def path(key, default):
            value = Path(values.get(key) or default).expanduser()
            return value if value.is_absolute() else root / value

        return cls(root, path("COLAB_STORAGE_DIR", "state/storage"),
                   path("COLAB_BACKUP_DIR", "state/backups"),
                   path("COLAB_SITE_DIR", ".local/site"),
                   path("COLAB_WORKING_DIR", "working_tmp"))
