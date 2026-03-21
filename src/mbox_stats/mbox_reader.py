

import email
from email.policy import default


class MboxReader:
    def __init__(self, handle):
        self._handle = handle
        assert self._handle.readline().startswith(b'From ')

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
            if line.startswith(b'>') and line.lstrip(b'>').startswith(b'From '):
                line = line[1:]
            lines.append(line)