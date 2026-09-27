"""Plugin Loader — dynamic module discovery for download providers.

Built-in download modules are *imported*, not read off the filesystem: in a
PyInstaller bundle the sources live inside the archive, so a `Path.exists()`
check on `omnidownloader/modules/*.py` fails and every module is silently
skipped.  That left the packaged app with zero modules — every URL routed to
UNKNOWN ("unknown source") and nothing could be downloaded.  External plugins
stay file-based, because those really are plain files in a directory.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Optional

from omnidownloader.core.base_module import BaseDownloaderModule

logger = logging.getLogger(__name__)

#: Built-in modules and their class name.  Order matters: the specialised
#: modules come first and HTTPDownloader last, because it is the catch-all
#: fallback for anything no other module claims.
BUILTIN_MODULES: tuple[tuple[str, str], ...] = (
    ("media_extractor", "MediaExtractor"),
    ("image_scraper", "ImageScraper"),
    ("torrent_downloader", "TorrentDownloader"),
    ("http_downloader", "HTTPDownloader"),
)


def user_plugin_dir() -> Path:
    """Directory users drop their own download modules into."""
    from omnidownloader.core import platform_utils

    return platform_utils.plugins_dir()


class PluginLoader:
    """Discover and instantiate download modules dynamically."""

    def __init__(self, plugin_dirs: Optional[list[str]] = None) -> None:
        self._plugin_dirs = [str(d) for d in (plugin_dirs or [])]

    def load_builtin_modules(self) -> list[BaseDownloaderModule]:
        """Import and instantiate the built-in modules.

        Works identically from a source checkout and from a frozen bundle: the
        import is the only requirement, so nothing depends on the .py file
        being visible on disk.
        """
        modules: list[BaseDownloaderModule] = []

        for module_name, class_name in BUILTIN_MODULES:
            try:
                mod = importlib.import_module(f"omnidownloader.modules.{module_name}")
            except Exception as exc:  # noqa: BLE001
                logger.error("Could not import built-in module %s: %s", module_name, exc)
                continue

            cls = getattr(mod, class_name, None)
            if cls is None:
                logger.warning("Class %s not found in %s", class_name, module_name)
                continue

            try:
                instance = cls()
            except Exception as exc:  # noqa: BLE001
                logger.error("Could not construct %s: %s", class_name, exc)
                continue

            if isinstance(instance, BaseDownloaderModule):
                modules.append(instance)
                logger.info("Loaded builtin module: %s", instance.display_name())
            else:
                logger.warning("%s does not subclass BaseDownloaderModule", class_name)

        return modules

    def load_external_plugins(self) -> list[BaseDownloaderModule]:
        """Discover and load external plugins from the plugin directories."""
        modules: list[BaseDownloaderModule] = []

        for plugin_dir in self._plugin_dirs:
            pdir = Path(plugin_dir).expanduser()
            if not pdir.is_dir():
                continue

            for py_file in sorted(pdir.glob("*.py")):
                if py_file.name.startswith("_"):
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(
                        f"plugin_{py_file.stem}", str(py_file)
                    )
                    if not (spec and spec.loader):
                        continue
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    for attr_name in dir(mod):
                        attr = getattr(mod, attr_name)
                        if (isinstance(attr, type)
                                and issubclass(attr, BaseDownloaderModule)
                                and attr is not BaseDownloaderModule):
                            try:
                                instance = attr()
                            except Exception as exc:  # noqa: BLE001
                                # A plugin with a required constructor argument
                                # must not take the whole app down.
                                logger.error(
                                    "Skipping plugin %s in %s: %s",
                                    attr_name, py_file.name, exc,
                                )
                                continue
                            modules.append(instance)
                            logger.info("Loaded external plugin: %s from %s",
                                        instance.display_name(), py_file)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to load plugin %s: %s", py_file, exc)

        return modules

    def load_all(self) -> list[BaseDownloaderModule]:
        """Load all builtin and external modules."""
        modules = self.load_builtin_modules()
        modules.extend(self.load_external_plugins())
        logger.info("Total modules loaded: %d", len(modules))
        if not modules:
            # Fail loudly: an app with no modules cannot route a single URL.
            logger.error(
                "No download modules could be loaded — every URL will be "
                "routed to UNKNOWN. This usually means a broken installation."
            )
        return modules
