"""One budget for all the heavy background work on this machine.

Three subsystems each parse and vectorize whole drawings — the ink cache
measures bounds, the optimize queue simplifies geometry, the plan queue
simulates the plot to estimate it. Each had its own worker thread, so
uploading three complex designs at once could put three long CPU-bound jobs
on a four-core Pi simultaneously, each with a subprocess of its own.

Memory was never the limit — a worst-case run peaks around 290MB with nearly
two gigabytes free. Sustained load is the limit. One 62-second job took the
board from 67C to 75C on its own; the Pi 4 begins throttling at 80C, and
sustained multi-core draw is also what browns out a marginal power supply,
which looks exactly like the unclean reboots being seen: no OOM record, no
clean shutdown, nothing in the log.

So two rules, and both of them also protect the plot itself:

- **At most one heavy job at a time.** Three parses do not finish three times
  faster on four cores; they finish at a third of the speed each while the
  event loop starves. Serial is not slower here, it is just honest about it.
- **Background work yields to the plotter.** These threads run at a lower
  scheduling priority than the plot worker and the event loop, so a drawing
  being measured can never compete with a pen that is moving. A starved plot
  thread means late serial writes, and late serial writes mean visible
  artefacts on paper.
"""
import contextlib
import logging
import os
import threading

log = logging.getLogger(__name__)

# One. Raising this trades plot smoothness and thermal headroom for background
# throughput, which is the wrong way round on a machine whose job is to hold a
# pen steady for three hours.
_HEAVY_SLOTS = 1
_heavy = threading.BoundedSemaphore(_HEAVY_SLOTS)

# How far below normal the background threads sit. 10 is enough that anything
# runnable preempts them, while still letting them use an otherwise idle core.
NICE = 10


def deprioritize() -> None:
    """Drop the *calling thread* below normal scheduling priority.

    On Linux niceness is per-task and threads are tasks, so this affects only
    the worker that calls it — the plot worker and the event loop keep theirs.
    Best effort: a kernel that refuses is not a reason to fail a measurement.
    """
    try:
        os.nice(NICE)
    except OSError:
        log.debug("workload: could not lower thread priority", exc_info=True)


@contextlib.contextmanager
def heavy(label: str = ""):
    """Hold the single heavy-work slot for the duration of the block."""
    _heavy.acquire()
    try:
        yield
    finally:
        _heavy.release()


def busy() -> bool:
    """True when the heavy slot is taken. Advisory — for status, not control."""
    if _heavy.acquire(blocking=False):
        _heavy.release()
        return False
    return True
