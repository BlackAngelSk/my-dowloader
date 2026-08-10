"""Plugin Loader — dynamic module discovery for download providers.

Discovers and loads download modules from the modules/ directory
and any third-party plugin directories specified in config.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Optional

from omnidownloader.core.base_module import BaseDownloaderModule

logger = logging.getLogger(__name__)


class PluginLoader:
    """Discover and instantiate download modules dynamically."""

    def __init__(self, plugin_dirs: Optional[list[str]] = None) -> None:
        self._plugin_dirs = [str(Path(__file__).parent.parent / "modules")]
        if plugin_dirs:
            self._plugin_dirs.extend(plugin_dirs)

    def load_builtin_modules(self) -> list[BaseDownloaderModule]:
        """Load all built-in modules from the modules/ directory."""
        modules: list[BaseDownloaderModule] = []

        # Map of module file → class to instantiate
        # Order matters: specialized modules FIRST, http_downloader LAST as fallback
        builtins = {
            "media_extractor": ("MediaExtractor", "MediaExtractor"),
            "image_scraper": ("ImageScraper", "ImageScraper"),
            "torrent_downloader": ("TorrentDownloader", "TorrentDownloader"),
            "http_downloader": ("HTTPDownloader", "HTTPDownloader"),
        }

        modules_dir = Path(__file__).parent.parent / "modules"
        for filename, (module_name, class_name) in builtins.items():
            filepath = modules_dir / f"{filename}.py"
            if not filepath.exists():
                logger.warning("Module file not found: %s", filepath)
                continue

            try:
                mod = importlib.import_module(f"omnidownloader.modules.{filename}")
                cls = getattr(mod, class_name, None)
                if cls is None:
                    logger.warning("Class %s not found in %s", class_name, filename)
                    continue

                instance = cls()
                if isinstance(instance, BaseDownloaderModule):
                    modules.append(instance)
                    logger.info("Loaded builtin module: %s", instance.display_name())
                else:
                    logger.warning(
                        "%s does not subclass BaseDownloaderModule", class_name
                    )
            except Exception as exc:
                logger.error("Failed to load module %s: %s", filename, exc)

        return modules

    def load_external_plugins(self) -> list[BaseDownloaderModule]:
        """Discover and load external plugins from plugin directories."""
        modules: list[BaseDownloaderModule] = []

        for plugin_dir in self._plugin_dirs[1:]:  # skip builtin dir
            pdir = Path(plugin_dir)
            if not pdir.exists():
                continue

            for py_file in pdir.glob("*.py"):
                if py_file.name.startswith("_"):
                    continue
                try:
                    spec = importlib.util.spec_from_file_location(
                        f"plugin_{py_file.stem}", str(py_file)
                    )
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        # Find any class that subclasses BaseDownloaderModule
                        for attr_name in dir(mod):
                            attr = getattr(mod, attr_name)
                            if (
                                isinstance(attr, type)
                                and issubclass(attr, BaseDownloaderModule)
                                and attr is not BaseDownloaderModule
                            ):
                                instance = attr()
                                modules.append(instance)
                                logger.info(
                                    "Loaded external plugin: %s from %s",
                                    instance.display_name(), py_file,
                                )
                except Exception as exc:
                    logger.error("Failed to load plugin %s: %s", py_file, exc)

        return modules

    def load_all(self) -> list[BaseDownloaderModule]:
        """Load all builtin and external modules."""
        modules = self.load_builtin_modules()
        modules.extend(self.load_external_plugins())
        logger.info("Total modules loaded: %d", len(modules))
        return modules
