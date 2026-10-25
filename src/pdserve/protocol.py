"""Small SSE parser and strict first-version completions request validation."""
import json
import math
import re
from .core import ServeError


def completion_options(body, model, request_id):
    if not isinstance(body, dict):
        raise ServeError(400, 'Expected a JSON object')
    allowed = {'model', 'prompt', 'max_tokens', 'stream', 'temperature', 'n', 'ignore_eos'}
    if set(body) - allowed:
        raise ServeError(400, 'Unsupported fields: ' + ', '.join(sorted(set(body) - allowed)))
    if body.get('model', model) != model:
        raise ServeError(404, 'Unknown served model')
    if not isinstance(body.get('prompt'), str) or not body['prompt']:
        raise ServeError(400, 'prompt must be one nonempty text string')
    if len(body['prompt'].encode('utf-8')) > 1_048_576:
        raise ServeError(413, 'Prompt exceeds the 1 MiB text limit')
    if type(body.get('n', 1)) is not int or body.get('n', 1) != 1:
        raise ServeError(400, 'Only n=1 is supported')
    output = body.get('max_tokens', 128)
    if type(output) is not int or output < 1:
        raise ServeError(400, 'max_tokens must be a positive integer')
    stream, ignore = body.get('stream', False), body.get('ignore_eos', False)
    if type(stream) is not bool or type(ignore) is not bool:
        raise ServeError(400, 'stream and ignore_eos must be booleans')
    temperature = body.get('temperature', 0)
    if (type(temperature) not in (int, float) or not math.isfinite(temperature)
            or not 0 <= temperature <= 2):
        raise ServeError(400, 'temperature must be finite and between 0 and 2')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', request_id):
        raise ServeError(400, 'X-Request-ID must contain 1 to 64 ASCII letters, digits, dots, underscores or hyphens')
    return {'output_limit': output, 'stream': stream, 'ignore_eos': ignore, 'temperature': temperature}


class SSEParser:
    def __init__(self):
        self.data = []
        self.bytes = 0

    def line(self, raw):
        self.bytes += len(raw)
        if self.bytes > 2_097_152:
            raise ServeError(502, 'Upstream SSE event exceeds 2 MiB')
        text = raw.decode('utf-8').rstrip('\r\n')
        if not text:
            data = '\n'.join(self.data)
            self.data, self.bytes = [], 0
            return data if data else None
        if text.startswith('data:'):
            self.data.append(text[5:].lstrip(' '))
        return None


def error_object(error):
    return {'error': {'message': str(error), 'type': 'pdserve_error', 'code': error.status}}


def frame_json(data):
    value = json.loads(data)
    if not isinstance(value, dict) or 'error' in value:
        raise ServeError(502, f'Upstream error frame: {value}')
    return value
