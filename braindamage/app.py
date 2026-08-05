"""Textual entrypoint for braindamage.

Four screens, navigated via F-keys (not letters — letters get swallowed by
whichever Input has focus, which is most of the time here). Maintenance,
Skins, and Contracts are stateless views recomputed from the database on every
visit (a fresh screen instance each time, so `on_mount` always reloads current
data). The contract builder is the one screen with real in-progress state — an
unfinished 10-input contract shouldn't vanish just because the user flipped to
the Skins screen to check a price — so it's a single long-lived instance
reused across navigation instead.
"""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from .screens.contract_builder import ContractBuilderScreen
from .screens.contracts import ContractsScreen
from .screens.maintenance import MaintenanceScreen
from .screens.skins import SkinsScreen


class BraindamageApp(App):
    TITLE = "braindamage"
    BINDINGS = [
        Binding("f1", "goto_maintenance", "Maintenance"),
        Binding("f2", "goto_skins", "Skins"),
        Binding("f3", "goto_builder", "Build Contract"),
        Binding("f4", "goto_contracts", "Contracts"),
    ]

    def on_mount(self) -> None:
        self._builder_screen = ContractBuilderScreen()
        self.push_screen(MaintenanceScreen())

    def action_goto_maintenance(self) -> None:
        self.switch_screen(MaintenanceScreen())

    def action_goto_skins(self) -> None:
        self.switch_screen(SkinsScreen())

    def action_goto_builder(self) -> None:
        self.switch_screen(self._builder_screen)

    def action_goto_contracts(self) -> None:
        self.switch_screen(ContractsScreen())


def main() -> None:
    BraindamageApp().run()


if __name__ == "__main__":
    main()
