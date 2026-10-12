"""Exact identity of the supported game; titles alone cannot select an app."""
import ntpath


def is_game_window(window):
    if (window is None or not isinstance(getattr(window, "title", None), str)
            or not isinstance(getattr(window, "executable", None), str)):
        return False
    title = " ".join(window.title.replace(":", " ").casefold().split())
    return (ntpath.basename(window.executable).casefold() == "genesis.exe"
            and title == "war of genesis idle loot")


def game_windows(windows):
    return [window for window in windows if is_game_window(window)]
