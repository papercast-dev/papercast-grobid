from setuptools import setup
from setuptools.command.install import install
import sys
import subprocess


class PostInstallCommand(install):
    def run(self):
        print("Running post-installation script...")
        install.run(self)

        plugin_module = "papercast_zotero"
        script_path = "generate_stubs.py"

        subprocess.run(
            [
                sys.executable,
                script_path,
                plugin_module,
            ],
            check=True,
        )


setup(
    cmdclass={"install": PostInstallCommand},
)
