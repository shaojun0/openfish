"""Index extension — watchdog-backed PackageIndex + PythonBuildIndex."""

from extensions import Extension
from index import register_all


class IndexExtension(Extension):
    name = "index"
    dependencies: list[str] = []

    def init_app(self, app) -> None:
        register_all(app)
