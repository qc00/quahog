import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class QuahogJSBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        js = Path(self.root, "js")
        subprocess.run(["pnpm", "install"], cwd=js, check=True)
        subprocess.run(["pnpm", "run", "build"], cwd=js, check=True)
