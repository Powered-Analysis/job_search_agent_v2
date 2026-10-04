class JsaError(Exception):
    """A failure the user can act on; the CLI prints its message and exits non-zero."""


def one_line(error: Exception) -> str:
    """The first line of an error's message, for the run log and the health line."""
    return (str(error).splitlines() or [""])[0] or type(error).__name__
