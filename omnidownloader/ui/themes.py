"""Theme engine — Dark/Light mode QSS stylesheets and palettes."""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass
class ThemeColors:
    bg_primary: str; bg_secondary: str; bg_card: str; bg_input: str
    bg_hover: str; bg_selected: str; text_primary: str; text_secondary: str
    text_muted: str; text_on_accent: str; accent: str; accent_hover: str
    accent_light: str; success: str; warning: str; error: str; info: str
    border: str; border_light: str; progress_bg: str; progress_fill: str
    scrollbar_bg: str; scrollbar_handle: str; shadow: str


DARK_COLORS = ThemeColors(
    bg_primary="#0F172A", bg_secondary="#1E293B", bg_card="#1E293B",
    bg_input="#334155", bg_hover="#334155", bg_selected="#3B82F6",
    text_primary="#F1F5F9", text_secondary="#CBD5E1", text_muted="#64748B",
    text_on_accent="#FFFFFF", accent="#3B82F6", accent_hover="#2563EB",
    accent_light="#1E3A5F", success="#14B8A6", warning="#F59E0B",
    error="#EF4444", info="#3B82F6", border="#334155", border_light="#475569",
    progress_bg="#334155", progress_fill="#3B82F6", scrollbar_bg="#1E293B",
    scrollbar_handle="#475569", shadow="rgba(0, 0, 0, 0.4)",
)

LIGHT_COLORS = ThemeColors(
    bg_primary="#F8FAFC", bg_secondary="#F1F5F9", bg_card="#FFFFFF",
    bg_input="#F1F5F9", bg_hover="#E2E8F0", bg_selected="#2563EB",
    text_primary="#0F172A", text_secondary="#475569", text_muted="#94A3B8",
    text_on_accent="#FFFFFF", accent="#2563EB", accent_hover="#1D4ED8",
    accent_light="#DBEAFE", success="#059669", warning="#D97706",
    error="#DC2626", info="#2563EB", border="#E2E8F0", border_light="#CBD5E1",
    progress_bg="#E2E8F0", progress_fill="#2563EB", scrollbar_bg="#F1F5F9",
    scrollbar_handle="#CBD5E1", shadow="rgba(0, 0, 0, 0.1)",
)


def get_palette(mode):
    return DARK_COLORS if mode == "dark" else LIGHT_COLORS

def get_system_theme():
    if sys.platform == "linux":
        try:
            import subprocess
            r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                               capture_output=True, text=True, timeout=2)
            if "dark" in r.stdout.lower():
                return "dark"
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if val == 1 else "dark"
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            import subprocess
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                               capture_output=True, text=True, timeout=2)
            if "dark" in r.stdout.lower():
                return "dark"
        except Exception:
            pass
    return "dark"



def generate_stylesheet(mode):
    c = get_palette(mode)
    return f"""
QMainWindow, QDialog {{ background-color: {c.bg_primary}; color: {c.text_primary}; }}
QWidget {{ color: {c.text_primary}; font-family: 'Segoe UI', 'Noto Sans', sans-serif; font-size: 13px; }}
#sidebar {{ background-color: {c.bg_secondary}; border-right: 1px solid {c.border}; min-width: 220px; max-width: 220px; }}
#sidebar QPushButton {{ background: transparent; border: none; border-radius: 8px; color: {c.text_secondary}; padding: 12px 16px; text-align: left; font-size: 14px; }}
#sidebar QPushButton:hover {{ background-color: {c.bg_hover}; color: {c.text_primary}; }}
#sidebar QPushButton[active="true"] {{ background-color: {c.accent_light}; color: {c.accent}; font-weight: bold; }}
#downloadCard {{ background-color: {c.bg_card}; border: 1px solid {c.border}; border-radius: 12px; padding: 16px; }}
#urlInput {{ background-color: {c.bg_input}; border: 2px solid {c.border}; border-radius: 10px; padding: 12px 16px; color: {c.text_primary}; font-size: 14px; }}
#urlInput:focus {{ border-color: {c.accent}; }}
QPushButton#primaryButton {{ background-color: {c.accent}; color: {c.text_on_accent}; border: none; border-radius: 8px; padding: 10px 24px; font-weight: bold; font-size: 14px; }}
QPushButton#primaryButton:hover {{ background-color: {c.accent_hover}; }}
QPushButton#iconButton {{ background: transparent; border: none; border-radius: 6px; padding: 8px; color: {c.text_secondary}; }}
QPushButton#iconButton:hover {{ background-color: {c.bg_hover}; color: {c.text_primary}; }}
QProgressBar {{ background-color: {c.progress_bg}; border: none; border-radius: 6px; height: 8px; }}
QProgressBar::chunk {{ background-color: {c.progress_fill}; border-radius: 6px; }}
QScrollBar:vertical {{ background: {c.scrollbar_bg}; width: 10px; border-radius: 5px; }}
QScrollBar::handle:vertical {{ background: {c.scrollbar_handle}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{ background-color: {c.bg_card}; color: {c.text_primary}; border: 1px solid {c.border}; border-radius: 6px; }}
QLabel#title {{ font-size: 20px; font-weight: bold; color: {c.text_primary}; background: transparent; }}
QLabel#subtitle {{ font-size: 13px; color: {c.text_secondary}; background: transparent; }}
QLabel#muted {{ color: {c.text_muted}; font-size: 12px; background: transparent; }}
#themeToggle {{ background: transparent; border: 2px solid {c.border}; border-radius: 20px; padding: 4px; }}
#themeToggle:hover {{ border-color: {c.accent}; }}
"""
