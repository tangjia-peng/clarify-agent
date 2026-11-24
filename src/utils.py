import sys


def log_title(message: str) -> None:
    title = f" {message} ".center(80, "=")
    if sys.stdout.isatty():
        title = f"\033[1;96m{title}\033[0m"
    print(title)
