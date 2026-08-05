"""Shared QRunnable signal holder.

QRunnable itself can't emit signals (it isn't a QObject), so every background
job in this app owns one of these, created on the main thread in the worker's
__init__ (before the runnable is handed to the thread pool) so the signals'
thread affinity is the UI thread and emissions from run() are auto-queued back
to it -- the Qt equivalent of Textual's `call_from_thread`.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal


class WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)
    progress = Signal(int, int)


def keep_alive(container: list[QRunnable], worker: QRunnable) -> None:
    """Holds a strong Python reference to `worker` in `container` until it
    settles (finished or error).

    QThreadPool's C++-side ownership of a submitted QRunnable does not, by
    itself, keep the Python wrapper -- and therefore the WorkerSignals QObject
    a caller stashed on it -- alive for the duration of a background run;
    without this, Python's GC can (and in practice does) collect the worker
    between `start()` returning and `run()` actually emitting on the pool
    thread, which raises "Signal source has been deleted".
    """
    container.append(worker)

    def _release(*_args: object) -> None:
        if worker in container:
            container.remove(worker)

    worker.signals.finished.connect(_release)
    worker.signals.error.connect(_release)
