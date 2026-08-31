"""The manual Pen up / Pen down buttons, and the stale state they must ignore.

The EBB cuts power to the lift servo after about a minute of idleness
(servo_timeout), so a pen parked up through a long pen change sags under
gravity while the board still answers "pen is up". The driver believes that
answer: connect() reads it back through find_pen_state(), and pen_raise() then
returns without sending anything because the pen is, as far as it knows,
already where it was asked to go. The button reports success and the pen stays
down — dead in precisely the situation that makes someone press it.

Nothing on the machine can report the servo's real angle, so the button does
not ask. It clears the driver's assumption and sends the command every time.
"""
import pytest

from app import plot_worker


class FakePen:
    """Stands in for pyaxidraw's PenHandler, with the one behaviour that
    matters here: a raise or a lower it believes to be redundant is skipped."""

    class Phys:
        z_up = None

    def __init__(self):
        self.phys = FakePen.Phys()


class FakeAxiDraw:
    def __init__(self, believes_up):
        self.pen = FakePen()
        self._believes_up = believes_up
        self.options = type("Options", (), {})()
        self.sent = []

    def interactive(self):
        pass

    def connect(self):
        # What find_pen_state() does with the board's answer.
        self.pen.phys.z_up = self._believes_up
        return True

    def penup(self):
        if self.pen.phys.z_up:
            return
        self.sent.append("up")
        self.pen.phys.z_up = True

    def pendown(self):
        if self.pen.phys.z_up is not None and not self.pen.phys.z_up:
            return
        self.sent.append("down")
        self.pen.phys.z_up = False

    def disconnect(self):
        pass


@pytest.fixture
def driver(monkeypatch):
    """Hand manual_pen a fake plotter whose starting belief each test sets."""
    made = []

    def make(believes_up):
        monkeypatch.setattr(plot_worker, "_current_ad", None)
        monkeypatch.setattr(plot_worker.axidraw, "AxiDraw",
                            lambda: made.append(FakeAxiDraw(believes_up)) or made[-1])
        return made

    return make


def test_pen_up_moves_the_pen_even_when_the_board_says_it_is_already_up(driver):
    made = driver(believes_up=True)
    plot_worker.manual_pen(raise_pen=True)
    assert made[-1].sent == ["up"]


def test_pen_down_moves_the_pen_even_when_the_board_says_it_is_already_down(driver):
    made = driver(believes_up=False)
    plot_worker.manual_pen(raise_pen=False)
    assert made[-1].sent == ["down"]


def test_pen_up_from_a_known_down_pen_still_raises_it(driver):
    made = driver(believes_up=False)
    plot_worker.manual_pen(raise_pen=True)
    assert made[-1].sent == ["up"]


def test_the_pen_is_still_refused_while_a_plot_is_driving_it(monkeypatch):
    monkeypatch.setattr(plot_worker, "_current_ad", object())
    with pytest.raises(RuntimeError, match="busy"):
        plot_worker.manual_pen(raise_pen=True)
