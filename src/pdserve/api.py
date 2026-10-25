"""HTTP surface. Streaming errors remain explicit SSE errors after headers."""
import asyncio
from contextlib import asynccontextmanager
import json
import secrets
from .core import ServeError
from .protocol import error_object


def build_app(service):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    # Request must be visible to FastAPI's annotation resolver in Python 3.10.
    globals()['Request'] = Request

    @asynccontextmanager
    async def lifespan(app):
        try:
            await service.start()
            yield
        finally:
            await service.close()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(ServeError)
    async def failure(request, error):
        return JSONResponse(error_object(error), status_code=error.status)

    async def body(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 2_097_152:
                raise ServeError(413, 'Request exceeds 2 MiB')
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise ServeError(400, 'Invalid JSON')

    class TrackedStream(StreamingResponse):
        def __init__(self, job, iterator):
            super().__init__(iterator, media_type='text/event-stream',
                headers={'X-Request-ID': job.request_id, 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
            self.job = job

        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                # Starlette's disconnect cancel scope must not swallow engine cancellation.
                if not self.job.reply_closed:
                    service.spawn(service.client_disconnected(self.job))

    async def events(job):
        while not job.finished.is_set() or not job.buffer.empty():
            try:
                chunk = await asyncio.wait_for(job.buffer.get(), .1)
                yield 'data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n'
            except asyncio.TimeoutError:
                continue
        if job.error:
            yield 'data: ' + json.dumps(error_object(job.error), ensure_ascii=False) + '\n\n'
        yield 'data: [DONE]\n\n'
        job.reply_closed = True

    async def wait_for_request(request, job, event):
        while not event.is_set():
            if await request.is_disconnected():
                await service.client_disconnected(job)
                raise ServeError(499, 'Client disconnected')
            try:
                await asyncio.wait_for(event.wait(), .1)
            except asyncio.TimeoutError:
                pass

    @app.post('/v1/completions')
    async def completions(request: Request):
        job = None
        try:
            job = await service.submit(await body(request), request.headers.get('X-Request-ID') or secrets.token_hex(16))
            await wait_for_request(request, job, job.started)
            if job.error:
                raise job.error
            if job.stream:
                return TrackedStream(job, events(job))
            await wait_for_request(request, job, job.finished)
            if job.error:
                raise job.error
            job.reply_closed = True
            return JSONResponse(service.completion(job), headers={'X-Request-ID': job.request_id})
        except asyncio.CancelledError:
            if job:
                service.spawn(service.client_disconnected(job))
            raise
        except Exception as error:
            if job:
                job.reply_closed = True
            elif isinstance(error, ServeError):
                service.telemetry.rejections[error.status] += 1
            raise

    @app.get('/health/live')
    async def live():
        return {'alive': True}

    @app.get('/health/ready')
    async def ready():
        return JSONResponse({'ready': service.ready, 'reason': service.reason}, status_code=200 if service.ready else 503)

    @app.get('/metrics')
    async def metrics():
        return Response(service.telemetry.render(service), media_type='text/plain; version=0.0.4')

    @app.get('/requests/{request_id}')
    async def request_state(request_id: str):
        return service.get(request_id).public(service.ledger)

    @app.post('/requests/{request_id}/cancel')
    async def cancel(request_id: str):
        return JSONResponse(await service.cancel(request_id), status_code=202)

    @app.get('/admin/state')
    async def state():
        return service.state()

    @app.post('/admin/recover')
    async def recover(request: Request):
        value = await body(request)
        if not isinstance(value, dict) or set(value) - {'old_pair_stopped'} or type(value.get('old_pair_stopped', False)) is not bool:
            raise ServeError(400, 'Expected optional boolean old_pair_stopped')
        return await service.recover(value.get('old_pair_stopped', False))

    return app
