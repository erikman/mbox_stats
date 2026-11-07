

import email
from email.policy import default


def unescape_from_line(line: bytes) -> bytes:
    if line.startswith(b'>') and line.lstrip(b'>').startswith(b'From '):
        return line[1:]
    return line


class MboxReader:
    def __init__(self, handle):
        self._handle = handle
        first_line = self._handle.readline()
        assert first_line.startswith(b'From ')
        self._pos = len(first_line)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self._handle.close()

    def __iter__(self):
        return iter(self.__next__())

    def __next__(self):
        lines = []
        while True:
            line = self._handle.readline()
            if line == b'' or line.startswith(b'From '):
                raw_message = b''.join(lines)
                yield email.message_from_bytes(raw_message, policy=default), len(raw_message)
                if line == b'':
                    break
                lines = []
                continue
            lines.append(unescape_from_line(line))

    def iter_with_offsets(self):
        """Yield (offset, raw_length, message_bytes); offset and raw_length refer to the decompressed mbox stream."""
        offset = self._pos
        lines = []
        while True:
            line = self._handle.readline()
            if line == b'' or line.startswith(b'From '):
                yield offset, self._pos - offset, b''.join(lines)
                if line == b'':
                    break
                self._pos += len(line)
                offset = self._pos
                lines = []
                continue
            self._pos += len(line)
            lines.append(unescape_from_line(line))