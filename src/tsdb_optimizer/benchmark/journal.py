"""Append-only, thread-safe raw evidence with deterministic hash chaining."""
import json
import threading
from pathlib import Path

from .protocol import checksum


class Journal:
    def __init__(self, path):
        self.stream = Path(path).open('x')
        self.lock = threading.Lock()
        self.index = 0
        self.previous = '0'*64

    def emit(self, kind, body):
        with self.lock:
            row = {'index': self.index, 'previous': self.previous, 'kind': kind, 'body': body}
            row['sha256'] = checksum(row)
            self.stream.write(json.dumps(row, allow_nan=False, separators=(',', ':'))+'\n')
            self.stream.flush()
            self.previous = row['sha256']
            self.index += 1

    def close(self):
        self.stream.close()


def read_events(path):
    previous = '0'*64
    terminal = False
    with Path(path).open() as stream:
        for index, line in enumerate(stream):
            row = json.loads(line)
            if terminal:
                raise ValueError('event after terminal record')
            if row.get('kind') == 'aborted' and row.get('body', {}).get('source') == 'process_supervisor':
                terminal = True
                yield row
                continue
            body = {k: v for k, v in row.items() if k != 'sha256'}
            if row.get('index') != index or row.get('previous') != previous or checksum(body) != row.get('sha256'):
                raise ValueError(f'journal integrity/order mismatch at event {index}')
            previous = row['sha256']
            terminal = row['kind'] in ('complete', 'aborted')
            yield row
