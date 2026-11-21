"""Console output that preserves progress logs on legacy Windows encodings."""
import sys


def console_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError as exc:
        encoding = getattr(sys.stdout, "encoding", None) or exc.encoding
        print(message.encode(encoding, errors="backslashreplace").decode(encoding))
