# SPDX-License-Identifier: LGPL-2.1+

from pathlib import Path

from mkosi.config import MkosiArgs, MkosiConfig
from mkosi.tree import make_tree
from mkosi.util import umask


class MkosiState:
    """State related properties."""

    def __init__(self, args: MkosiArgs, config: MkosiConfig, workspace: Path) -> None:
        self.args = args
        self.config = config
        self.workspace = workspace

        with umask(~0o755):
            make_tree(self.config, self.root)
        self.staging.mkdir()
        self.pkgmngr.mkdir()
        self.install_dir.mkdir(exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self.workspace / "root"

    @property
    def staging(self) -> Path:
        return self.workspace / "staging"

    @property
    def pkgmngr(self) -> Path:
        return self.workspace / "pkgmngr"

    @property
    def cache_dir(self) -> Path:
        return self.config.cache_dir or self.workspace / f"cache/{self.config.distribution}~{self.config.release}"

    @property
    def install_dir(self) -> Path:
        return self.workspace / "dest"
