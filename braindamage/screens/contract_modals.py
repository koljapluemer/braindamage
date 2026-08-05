"""Modal dialogs used by the contract builder screen to add/edit one trade-up
input line: a search-first skin picker, then a small float/quantity form —
command-palette-style rather than one big form, so picking from ~thousands of
eligible skins stays fast.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList
from textual.widgets.option_list import Option

from ..tradeup import SkinOption


class SkinPickerModal(ModalScreen[SkinOption | None]):
    """Live-filtered search over every eligible trade-up input. Enter picks the
    highlighted match (defaulting to the top one); Escape cancels."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    SkinPickerModal {
        align: center middle;
    }
    SkinPickerModal > Vertical {
        width: 80%;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    SkinPickerModal OptionList {
        height: 1fr;
    }
    """

    def __init__(self, options: list[SkinOption]) -> None:
        super().__init__()
        self._all_options = options
        self._by_id = {option.skin_id: option for option in options}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Add trade-up input — type to search, Enter to pick, Esc to cancel")
            yield Input(placeholder="Search skins...", id="picker_search")
            yield OptionList(id="picker_options")

    def on_mount(self) -> None:
        self._populate(self._all_options)
        self.query_one("#picker_search", Input).focus()

    def _populate(self, options: list[SkinOption]) -> None:
        option_list = self.query_one("#picker_options", OptionList)
        option_list.clear_options()
        for option in options[:200]:
            option_list.add_option(Option(option.label, id=option.skin_id))

    def on_input_changed(self, event: Input.Changed) -> None:
        needle = event.value.strip().lower()
        options = self._all_options if not needle else [o for o in self._all_options if needle in o.label.lower()]
        self._populate(options)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        option_list = self.query_one("#picker_options", OptionList)
        highlighted = option_list.highlighted_option
        if highlighted is not None and highlighted.id is not None:
            self.dismiss(self._by_id.get(highlighted.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss(self._by_id.get(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class LineDetailModal(ModalScreen[tuple[float, int] | None]):
    """Float value + quantity for one contract line, validated against the
    skin's float range and the contract's remaining input slots."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    LineDetailModal {
        align: center middle;
    }
    LineDetailModal > Vertical {
        width: 60;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    LineDetailModal #detail_error {
        color: $error;
    }
    """

    def __init__(
        self,
        option: SkinOption,
        max_quantity: int,
        initial_float: float | None = None,
        initial_quantity: int = 1,
    ) -> None:
        super().__init__()
        self._option = option
        self._max_quantity = max_quantity
        self._initial_float = initial_float if initial_float is not None else (option.min_float + option.max_float) / 2
        self._initial_quantity = max(1, min(initial_quantity, max_quantity))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._option.label)
            yield Label(f"Float range: {self._option.min_float:.4f} – {self._option.max_float:.4f}")
            yield Label("Float value")
            yield Input(value=f"{self._initial_float:.4f}", id="float_input")
            yield Label(f"Quantity (max {self._max_quantity})")
            yield Input(value=str(self._initial_quantity), id="qty_input")
            yield Label("", id="detail_error")
            with Horizontal():
                yield Button("Confirm", id="confirm", variant="success")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#float_input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self._confirm()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._confirm()

    def _confirm(self) -> None:
        error = self.query_one("#detail_error", Label)
        try:
            float_value = float(self.query_one("#float_input", Input).value)
            quantity = int(self.query_one("#qty_input", Input).value)
        except ValueError:
            error.update("Float and quantity must be numbers.")
            return
        if not (self._option.min_float <= float_value <= self._option.max_float):
            error.update(f"Float must be within {self._option.min_float:.4f}-{self._option.max_float:.4f}.")
            return
        if not (1 <= quantity <= self._max_quantity):
            error.update(f"Quantity must be between 1 and {self._max_quantity}.")
            return
        self.dismiss((float_value, quantity))

    def action_cancel(self) -> None:
        self.dismiss(None)
