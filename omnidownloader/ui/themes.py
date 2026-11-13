"""Theme engine — palettes and the application stylesheet.

Two palettes (graphite dark, paper light) that share one design system: a 14 px
card radius, 1 px borders, a periwinkle accent, and status colours that only
ever appear as pills, dots and progress fills.  The stylesheet below is the
single place where colours and metrics are decided; widgets ask for object
names (``#downloadCard``, ``#pill[tone="active"]``) instead of hard-coding hex
values, so switching theme restyles everything — including the widgets that
used to paint their own dark colours on a light window.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from string import Template

from omnidownloader.core import platform_utils


@dataclass(frozen=True)
class ThemeColors:
    """Every colour the interface uses, by role."""

    # surfaces
    bg: str
    sidebar: str
    surface: str
    surface_2: str
    surface_3: str
    # ink
    text: str
    text_2: str
    text_3: str
    on_accent: str
    # lines
    border: str
    border_strong: str
    # accent
    accent: str
    accent_hover: str
    accent_soft: str
    # status
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    # progress + chrome
    track: str
    fill_a: str
    fill_b: str
    scroll: str
    scroll_hover: str
    shadow: str
    scrim: str
    stage: str

    # ── compatibility aliases (older widgets read these names) ──
    @property
    def bg_primary(self) -> str:
        return self.bg

    @property
    def bg_secondary(self) -> str:
        return self.sidebar

    @property
    def bg_card(self) -> str:
        return self.surface

    @property
    def bg_input(self) -> str:
        return self.surface_2

    @property
    def bg_hover(self) -> str:
        return self.surface_2

    @property
    def text_primary(self) -> str:
        return self.text

    @property
    def text_secondary(self) -> str:
        return self.text_2

    @property
    def text_muted(self) -> str:
        return self.text_3

    @property
    def text_on_accent(self) -> str:
        return self.on_accent

    @property
    def accent_light(self) -> str:
        return self.accent_soft

    @property
    def error(self) -> str:
        return self.danger

    @property
    def info(self) -> str:
        return self.accent

    @property
    def progress_bg(self) -> str:
        return self.track

    @property
    def progress_fill(self) -> str:
        return self.fill_a

    @property
    def scrollbar_bg(self) -> str:
        return self.bg

    @property
    def scrollbar_handle(self) -> str:
        return self.scroll

    @property
    def bg_selected(self) -> str:
        return self.accent

    @property
    def border_light(self) -> str:
        return self.border_strong


DARK_COLORS = ThemeColors(
    bg="#0A0C11",
    sidebar="#0D1016",
    surface="#12161F",
    surface_2="#171C26",
    surface_3="#1F2531",
    text="#E8ECF3",
    text_2="#9AA5B5",
    text_3="#68727F",
    on_accent="#0A0C11",
    border="#212734",
    border_strong="#2E3644",
    accent="#7C8CFF",
    accent_hover="#98A5FF",
    accent_soft="rgba(124, 140, 255, 0.16)",
    success="#4ADE9B",
    success_soft="rgba(74, 222, 155, 0.14)",
    warning="#F5C05C",
    warning_soft="rgba(245, 192, 92, 0.14)",
    danger="#FF6B7A",
    danger_soft="rgba(255, 107, 122, 0.14)",
    track="#1E2431",
    fill_a="#7C8CFF",
    fill_b="#B07BFF",
    scroll="#242B39",
    scroll_hover="#333C4E",
    shadow="rgba(0, 0, 0, 0.45)",
    scrim="rgba(10, 12, 17, 0.88)",
    stage="#0B0E14",
)

LIGHT_COLORS = ThemeColors(
    bg="#F5F6F9",
    sidebar="#FFFFFF",
    surface="#FFFFFF",
    surface_2="#F1F3F8",
    surface_3="#E7EAF1",
    text="#131720",
    text_2="#525C6B",
    text_3="#8B94A3",
    on_accent="#FFFFFF",
    border="#E3E7EE",
    border_strong="#CCD3DF",
    accent="#5A6CF6",
    accent_hover="#4757E6",
    accent_soft="rgba(90, 108, 246, 0.10)",
    success="#0F9D64",
    success_soft="rgba(15, 157, 100, 0.12)",
    warning="#B4780A",
    warning_soft="rgba(180, 120, 10, 0.12)",
    danger="#D93B4C",
    danger_soft="rgba(217, 59, 76, 0.12)",
    track="#E6E9F0",
    fill_a="#5A6CF6",
    fill_b="#9061F5",
    scroll="#D2D8E3",
    scroll_hover="#BBC3D1",
    shadow="rgba(15, 23, 42, 0.10)",
    scrim="rgba(255, 255, 255, 0.92)",
    stage="#12161F",
)


def get_palette(mode):
    return DARK_COLORS if mode == "dark" else LIGHT_COLORS


#: The mode currently applied to the window.  Widgets that paint their own
#: icons (cards, the sidebar) need colours at construction and on theme change,
#: which the stylesheet cannot give them.
_current_mode = "dark"


def set_current_mode(mode: str) -> None:
    global _current_mode
    _current_mode = "light" if mode == "light" else "dark"


def current_mode() -> str:
    return _current_mode


def current_palette() -> ThemeColors:
    return get_palette(_current_mode)


def get_system_theme():
    """The desktop's light/dark preference, or "dark" when it cannot be read.

    The OS branches go through ``platform_utils`` so platform identity lives in
    exactly one place (and macOS/Linux get their subprocess timeouts, which a
    hung ``gsettings`` used to make the whole startup wait for).
    """
    system = platform_utils.system()
    if system == platform_utils.LINUX:
        # GNOME/GTK first; KDE exposes it through the same portal-ish query.
        for cmd in (
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
        ):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        timeout=2, **platform_utils.subprocess_kwargs())
            except (OSError, subprocess.SubprocessError):
                continue
            output = (result.stdout or "").lower()
            if "dark" in output:
                return "dark"
            if "light" in output:
                return "light"
    elif system == platform_utils.WINDOWS:  # pragma: no cover - Windows only
        try:
            import winreg
            key = winreg.OpenKey(  # type: ignore[attr-defined]
                winreg.HKEY_CURRENT_USER,  # type: ignore[attr-defined]
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")  # type: ignore[attr-defined]
            winreg.CloseKey(key)  # type: ignore[attr-defined]
            return "light" if val == 1 else "dark"
        except Exception:  # noqa: BLE001
            pass
    elif system == platform_utils.MACOS:  # pragma: no cover - macOS only
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2,
                **platform_utils.subprocess_kwargs())
            if "dark" in (result.stdout or "").lower():
                return "dark"
        except (OSError, subprocess.SubprocessError):
            pass
    return "dark"


def qt_palette(mode: str):
    """A QPalette matching the theme.

    Native pieces Qt draws itself (the combo-box arrow, spin-box buttons,
    checkbox indicators, tooltips, selection colour) do not read the stylesheet,
    which is why an unthemed combo box showed up as a white box on the dark
    window.  A palette fixes them all at once.
    """
    from PyQt6.QtGui import QColor, QPalette

    c = get_palette(mode)
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: c.bg,
        QPalette.ColorRole.WindowText: c.text,
        QPalette.ColorRole.Base: c.surface_2,
        QPalette.ColorRole.AlternateBase: c.surface,
        QPalette.ColorRole.Text: c.text,
        QPalette.ColorRole.Button: c.surface,
        QPalette.ColorRole.ButtonText: c.text,
        QPalette.ColorRole.Highlight: c.accent,
        QPalette.ColorRole.HighlightedText: c.on_accent,
        QPalette.ColorRole.ToolTipBase: c.surface_3,
        QPalette.ColorRole.ToolTipText: c.text,
        QPalette.ColorRole.PlaceholderText: c.text_3,
        QPalette.ColorRole.Link: c.accent,
        QPalette.ColorRole.BrightText: c.danger,
    }
    for role, value in roles.items():
        pal.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(c.text_3))
    return pal


_STYLESHEET = Template("""
/* ── base ───────────────────────────────────────────────────── */
QWidget {
    color: $text;
    font-family: "Inter", "Segoe UI Variable Text", "Segoe UI", "Noto Sans",
                 "DejaVu Sans", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog, #appShell { background-color: $bg; }
QToolTip {
    background-color: $surface_3; color: $text;
    border: 1px solid $border_strong; border-radius: 8px; padding: 6px 8px;
}

/* ── sidebar ────────────────────────────────────────────────── */
#sidebar {
    background-color: $sidebar;
    border-right: 1px solid $border;
}
#logoText { font-size: 14px; font-weight: 700; color: $text; }
#logoSub  { font-size: 10px; color: $text_3; }
#navBtn {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    color: $text_2;
    padding: 9px 10px;
    text-align: left;
    font-size: 13px;
}
#navBtn:hover { background-color: $surface_2; color: $text; }
#navBtn[active="true"] {
    background-color: $accent_soft;
    border-color: rgba(124, 140, 255, 0.22);
    color: $text;
    font-weight: 600;
}
#navSection {
    color: $text_3; font-size: 10px; font-weight: 600;
    letter-spacing: 1px; padding: 4px 10px;
}
#sidebarFooter { color: $text_3; font-size: 11px; }

/* ── typography ─────────────────────────────────────────────── */
QLabel#pageTitle { font-size: 21px; font-weight: 700; color: $text; }
QLabel#title     { font-size: 21px; font-weight: 700; color: $text; }
QLabel#pageSubtitle { font-size: 12px; color: $text_3; }
QLabel#subtitle  { font-size: 12px; color: $text_3; }
QLabel#muted     { font-size: 11px; color: $text_3; }
QLabel#sectionLabel {
    font-size: 10px; font-weight: 600; color: $text_3; letter-spacing: 1px;
}

/* ── page header + stats ────────────────────────────────────── */
#pageHeader { background: transparent; }
#statChip {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 12px;
}
#statValue { font-size: 15px; font-weight: 600; color: $text; }
#statLabel { font-size: 10px; color: $text_3; }
#cardPanel {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 14px;
}

/* ── command bar ────────────────────────────────────────────── */
QLineEdit#urlInput {
    background-color: $surface;
    border: 1px solid $border_strong;
    border-radius: 12px;
    padding: 0 14px;
    color: $text;
    font-size: 13px;
    selection-background-color: $accent;
    selection-color: $on_accent;
}
QLineEdit#urlInput:focus { border-color: $accent; background-color: $surface_2; }

/* ── buttons ────────────────────────────────────────────────── */
QPushButton#primaryButton {
    background-color: $accent;
    color: $on_accent;
    border: none;
    border-radius: 11px;
    padding: 10px 20px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#primaryButton:hover  { background-color: $accent_hover; }
QPushButton#primaryButton:disabled { background-color: $surface_3; color: $text_3; }

QPushButton#ghostButton {
    background-color: transparent;
    border: 1px solid $border_strong;
    border-radius: 11px;
    padding: 9px 14px;
    color: $text_2;
    font-size: 13px;
}
QPushButton#ghostButton:hover { background-color: $surface_2; color: $text; }
QPushButton#ghostButton:disabled { color: $text_3; border-color: $border; }

QPushButton#cardAction {
    background: transparent; border: none; border-radius: 8px; padding: 0;
}
QPushButton#cardAction:hover { background-color: $surface_3; }
QPushButton#cardAction:disabled { background: transparent; }

QPushButton#filterChip {
    background-color: transparent;
    border: 1px solid $border;
    border-radius: 14px;
    padding: 5px 13px;
    color: $text_2;
    font-size: 12px;
}
QPushButton#filterChip:hover { background-color: $surface_2; color: $text; }
QPushButton#filterChip:checked {
    background-color: $accent_soft;
    border-color: $accent;
    color: $text;
    font-weight: 600;
}

/* ── download card ──────────────────────────────────────────── */
#downloadCard {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 14px;
}
#downloadCard:hover { border-color: $border_strong; }
#cardTitle { font-size: 13px; font-weight: 600; color: $text; }
#cardSub   { font-size: 11px; color: $text_3; }
#cardMeta  { font-size: 11px; color: $text_2; }
#cardPercent { font-size: 12px; font-weight: 600; color: $text; }
#fileTile {
    border-radius: 10px;
    font-size: 9px;
    font-weight: 700;
    color: $text_2;
    background-color: $surface_3;
}
#pill {
    border-radius: 9px;
    padding: 3px 9px;
    font-size: 10px;
    font-weight: 600;
}
#pill[tone="active"] { background-color: $accent_soft;  color: $accent; }
#pill[tone="done"]   { background-color: $success_soft; color: $success; }
#pill[tone="warn"]   { background-color: $warning_soft; color: $warning; }
#pill[tone="error"]  { background-color: $danger_soft;  color: $danger; }
#pill[tone="idle"]   { background-color: $surface_3;    color: $text_2; }
#qualityPill {
    background-color: $surface_3; color: $text_2;
    border-radius: 9px; padding: 3px 8px; font-size: 10px; font-weight: 600;
}
QProgressBar#cardProgress {
    background-color: $track;
    border: none;
    border-radius: 4px;
}
QProgressBar#cardProgress::chunk {
    border-radius: 4px;
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 $fill_a, stop:1 $fill_b);
}

/* ── empty state ────────────────────────────────────────────── */
#emptyTitle { font-size: 14px; font-weight: 600; color: $text_2; }
#emptyHint  { font-size: 12px; color: $text_3; }

/* ── forms (settings, scheduler, anonymity) ─────────────────── */
QGroupBox {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 14px;
    margin-top: 0px;
    padding: 14px 16px 16px 16px;
    font-size: 12px;
}
QGroupBox::title {
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 2px;
    top: 0px;
    padding: 0 0 8px 0;
    color: $text_2;
    font-size: 11px;
    font-weight: 600;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: $surface_2;
    border: 1px solid $border;
    border-radius: 9px;
    padding: 7px 10px;
    color: $text;
    min-height: 18px;
    selection-background-color: $accent;
    selection-color: $on_accent;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: $accent;
}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color: $text_3; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: $surface_2;
    border: 1px solid $border_strong;
    border-radius: 10px;
    padding: 4px;
    selection-background-color: $accent_soft;
    selection-color: $text;
    outline: none;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 16px; border: none; }
QCheckBox { spacing: 8px; color: $text_2; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid $border_strong;
    border-radius: 5px;
    background-color: $surface_2;
}
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked { background-color: $accent; border-color: $accent; }
QRadioButton { spacing: 8px; color: $text_2; }
QPushButton {
    background-color: $surface_2;
    border: 1px solid $border;
    border-radius: 9px;
    padding: 7px 14px;
    color: $text;
}
QPushButton:hover { background-color: $surface_3; border-color: $border_strong; }
QPushButton:disabled { color: $text_3; }
QPushButton:checked {
    background-color: $accent_soft;
    border-color: $accent;
    color: $text;
    font-weight: 600;
}
QProgressBar {
    background-color: $track; border: none; border-radius: 4px; height: 8px;
}
QProgressBar::chunk {
    border-radius: 4px;
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 $fill_a, stop:1 $fill_b);
}

/* ── tabs + lists (format dialog) ───────────────────────────── */
QTabWidget::pane {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 12px;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    color: $text_3;
    border: none;
    border-radius: 9px;
    padding: 7px 14px;
    margin-right: 6px;
    font-size: 12px;
}
QTabBar::tab:hover { background-color: $surface_2; color: $text_2; }
QTabBar::tab:selected {
    background-color: $accent_soft;
    color: $text;
    font-weight: 600;
}
QListWidget {
    background: transparent;
    border: none;
    outline: none;
    color: $text;
}
QListWidget::item {
    border-radius: 10px;
    margin: 2px 0;
    padding: 0;
}
QListWidget::item:hover { background-color: $surface_2; }
QListWidget::item:selected { background-color: $accent_soft; color: $text; }

/* ── scrollbars ─────────────────────────────────────────────── */
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 2px;
}
QScrollBar::handle:vertical {
    background: $scroll; border-radius: 4px; min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: $scroll_hover; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal {
    background: $scroll; border-radius: 4px; min-width: 32px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ── misc widgets that predate the redesign ─────────────────── */
#themeToggle {
    background-color: transparent;
    border: 1px solid $border;
    border-radius: 10px;
    padding: 8px 10px;
    color: $text_2;
    text-align: left;
}
#themeToggle:hover { background-color: $surface_2; color: $text; }
#divider { background-color: $border; border: none; }
QFrame[HLine="true"] { background-color: $border; }
#statusDot { border-radius: 4px; }
#dropZone {
    background-color: $scrim;
    border: 2px dashed $accent;
    border-radius: 22px;
    color: $text;
    font-size: 18px;
    font-weight: 600;
    padding: 60px;
}
#urlIcon { background: transparent; }
""")


def generate_stylesheet(mode: str) -> str:
    """The full QSS for *mode* ("dark" or "light")."""
    colors = get_palette(mode)
    from dataclasses import asdict

    values = asdict(colors)
    values.pop("shadow", None)
    return _STYLESHEET.safe_substitute(values)
