"""Small colored chip for a rarity name, backed by tradeup.RARITY_LADDER's hex
colors -- data-driven styling that the previous (Textual) UI never used for
anything visual.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel


class RarityBadge(QLabel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._apply_style(None)

    def set_rarity(self, rarity_name: str | None, color_hex: str | None) -> None:
        self.setText(rarity_name or "—")
        self._apply_style(color_hex)

    def _apply_style(self, color_hex: str | None) -> None:
        if color_hex is None:
            self.setStyleSheet(
                "padding: 2px 8px; border-radius: 4px; background: palette(mid); color: palette(window-text);"
            )
            return
        text_color = "#1a1a1a" if _is_light(color_hex) else "#ffffff"
        self.setStyleSheet(
            f"padding: 2px 8px; border-radius: 4px; background: {color_hex}; "
            f"color: {text_color}; font-weight: 600;"
        )


def _is_light(color_hex: str) -> bool:
    color_hex = color_hex.lstrip("#")
    if len(color_hex) != 6:
        return False
    r, g, b = (int(color_hex[i : i + 2], 16) for i in (0, 2, 4))
    luminance = 0.299 * r + 0.587 * g + 0.114 * b  # perceived brightness, standard weights
    return luminance > 150
