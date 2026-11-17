from __future__ import annotations

import re


# Standard ANSI terminal escape sequences
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_ITALIC = "\033[3m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_WHITE = "\033[37m"

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def fg_rgb(r: int, g: int, b: int) -> str:
    """Return 24-bit TrueColor foreground ANSI escape sequence."""
    return f"\033[38;2;{r};{g};{b}m"


def bg_rgb(r: int, g: int, b: int) -> str:
    """Return 24-bit TrueColor background ANSI escape sequence."""
    return f"\033[48;2;{r};{g};{b}m"


def colorize_rgb(
    text: str,
    fg: tuple[int, int, int] | None = None,
    bg: tuple[int, int, int] | None = None,
    bold: bool = False,
    dim: bool = False,
    enabled: bool = True,
) -> str:
    if not enabled:
        return text
    codes: list[str] = []
    if bold:
        codes.append(ANSI_BOLD)
    if dim:
        codes.append(ANSI_DIM)
    if fg is not None:
        codes.append(fg_rgb(*fg))
    if bg is not None:
        codes.append(bg_rgb(*bg))
    if not codes:
        return text
    return f"{''.join(codes)}{text}{ANSI_RESET}"


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text for accurate column width calculations."""
    return ANSI_ESCAPE_RE.sub("", text)


def _pad_visible(content: str, width: int, align: str = "left") -> str:
    """Pad content with spaces to match visible character width."""
    vis_len = len(strip_ansi(content))
    pad = max(0, width - vis_len)
    if align == "right":
        return (" " * pad) + content
    if align == "center":
        left = pad // 2
        right = pad - left
        return (" " * left) + content + (" " * right)
    return content + (" " * pad)


def _truncate_visible(text: str, max_width: int, ellipsis: str = "...") -> str:
    plain = strip_ansi(text)
    if len(plain) <= max_width:
        return text
    if max_width <= len(ellipsis):
        return plain[:max_width]
    return plain[: max_width - len(ellipsis)] + ellipsis
