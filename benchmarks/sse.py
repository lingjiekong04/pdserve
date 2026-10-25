"""Bounded SSE parser shared by the measurement client."""
import json


class Parser:
    def __init__(self, limit=2_097_152):
        self.data = []
        self.bytes = 0
        self.limit = limit

    def line(self, raw):
        self.bytes += len(raw)
        if self.bytes > self.limit:
            raise ValueError('SSE event exceeds size limit')
        text = raw.decode('utf-8').rstrip('\r\n')
        if not text:
            data = '\n'.join(self.data)
            self.data = []
            self.bytes = 0
            return data if data else None
        if text.startswith('data:'):
            self.data.append(text[5:].lstrip(' '))
        return None


def frame(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('SSE data must be a JSON object')
    return value
