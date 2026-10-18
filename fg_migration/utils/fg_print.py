"""print functions"""

GLOBAL_ERROR_COUNT = 0
GLOBAL_ERROR_LIST = []

class Bcolors:
    """Color definitions for console output"""

    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    DIM = "\033[2m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


IS_DEBUG:bool = False


def message(color, msg, colorend=Bcolors.ENDC, bold=False) -> str:
    """Returns a message in color"""
    if bold:
        return Bcolors.BOLD + message(color, msg, colorend, False)

    return color + msg + colorend


def print_color(color, msg, colorend=Bcolors.ENDC, _bold=False) -> None:
    """Prints a message in color"""
    # flush so progress shows up immediately, also when output is piped or logged
    print(message(color, msg, colorend), flush=True)

def debug(msg) -> None:
    """Prints a debug message"""
    if IS_DEBUG:
        print_color(Bcolors.DIM, msg)

def info(msg) -> None:
    """Prints an info message"""
    print_color(Bcolors.OKBLUE, msg)


def progress(index:int, total:int|None, msg:str) -> None:
    """Prints a progress message, e.g. [3/40] msg (or [3] msg if the total is unknown)"""
    prefix = f"[{index}/{total}]" if total else f"[{index}]"
    info(f"{prefix} {msg}")


def section(msg) -> None:
    """Prints a section header for a migration phase"""
    print_color(Bcolors.HEADER, f"\n--- {msg} ---")


def format_duration(seconds:float) -> str:
    """Formats seconds as a human readable duration"""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def success(msg) -> None:
    """Prints a success message"""
    print_color(Bcolors.OKGREEN, msg)


def warning(msg) -> None:
    """Prints a warning message"""
    print_color(Bcolors.WARNING, msg)


def error(msg:str, element:str=None) -> int:
    """Prints an error message and increments the global error count"""
    global GLOBAL_ERROR_COUNT  # pylint: disable=global-statement
    global GLOBAL_ERROR_LIST  # pylint: disable=global-variable-not-assigned
    if element:
        GLOBAL_ERROR_LIST.append(element)
    else:
        GLOBAL_ERROR_LIST.append(msg)
    GLOBAL_ERROR_COUNT += 1
    print_color(Bcolors.FAIL, msg)
    return GLOBAL_ERROR_COUNT
