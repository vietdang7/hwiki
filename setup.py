"""Build hook: ship framework/skills/** (operator skill) and framework/benches/** inside
the wheel as hermes_wikiskill/data/{skills,benches} (non-editable installs, install.sh).
Editable/source runs fall back to the repo paths (see hermes_wikiskill/resources.py)."""

import os
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

HERE = os.path.dirname(os.path.abspath(__file__))


class BuildWithData(build_py):
    def run(self):
        super().run()
        for src, dst in (("skills", "skills"), ("benches", "benches")):
            s = os.path.join(HERE, src)
            if os.path.isdir(s):
                shutil.copytree(s, os.path.join(self.build_lib, "hermes_wikiskill", "data", dst),
                                dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".*"))


setup(cmdclass={"build_py": BuildWithData})
