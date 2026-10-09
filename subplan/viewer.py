from contextlib import contextmanager
import codecs
import os
import select
import shutil
import sys
import threading
import time

from .display import text_view
from .model import UsageError
from .service import cached, refresh
from .storage import load_config
from .ui import DockUI, InputDecoder


@contextmanager
def keyboard():
    if os.name == "nt":
        import msvcrt

        def read():
            text = ""
            while msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    char = {"K": "\x1b[D", "M": "\x1b[C", "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~"}.get(msvcrt.getwch(), "")
                text += char
            return text
        yield read
    else:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")

            def read():
                return decoder.decode(os.read(fd, 4096)) if select.select([fd], [], [], 0)[0] else ""
            yield read
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def view(docked=False):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(text_view(refresh()))
        return
    closed = threading.Event()
    requested = threading.Event()

    def refresh_worker():
        while not closed.is_set():
            if not requested.wait(0.2):
                continue
            requested.clear()
            try:
                refresh(blocking=True)
            except UsageError:
                pass

    def request_refresh():
        requested.set()

    threading.Thread(target=refresh_worker, daemon=True).start()

    pane = os.environ.get("HERDR_PANE_ID")

    def toggle(collapsed):
        if pane:
            from .dock import set_collapsed
            set_collapsed(pane, collapsed)

    def close_pane():
        if pane:
            from .dock import hide
            hide(pane)
        closed.set()

    ui = DockUI(toggle, request_refresh, close_pane)
    decoder = InputDecoder()

    def updater():
        while not closed.is_set():
            try:
                config = load_config()
                if not docked and time.time() - cached().get("queried_at", 0) >= config["refresh_seconds"]:
                    refresh()
            except Exception:
                pass
            closed.wait(1)

    threading.Thread(target=updater, daemon=True).start()
    try:
        with keyboard() as read:
            if docked:
                sys.stdout.write("\x1b[?1000h\x1b[?1006h\x1b[?2004h")
                sys.stdout.flush()
            previous = None
            painted_at = 0
            while True:
                if closed.is_set():
                    return
                try:
                    size = os.get_terminal_size(sys.stdout.fileno())
                except OSError:
                    size = shutil.get_terminal_size()
                if docked and pane:
                    from .dock import pane_preferences
                    ui.collapsed = bool(pane_preferences(pane).get("collapsed"))
                text = ui.frame(cached(), size.columns, size.lines) if docked else "\x1b[2J\x1b[H" + text_view(cached()).replace("\n", "\r\n")
                if text != previous or time.monotonic() - painted_at >= 1:
                    # The terminal popup owns these escape sequences; tokens sent
                    # to the native sidebar are plain text only.
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    previous = text
                    painted_at = time.monotonic()
                events = decoder.feed(read()) + decoder.flush_escape()
                for event in events:
                    if docked:
                        ui.event(event)
                    elif event == ("key", "escape") or event in (("text", "q"), ("text", "Q")):
                        return
                    elif event in (("text", "r"), ("text", "R")):
                        request_refresh()
                time.sleep(0.03)
    finally:
        closed.set()
        sys.stdout.write("\x1b[?1000l\x1b[?1006l\x1b[?2004l\x1b[?25h\x1b[0m")
        sys.stdout.flush()
