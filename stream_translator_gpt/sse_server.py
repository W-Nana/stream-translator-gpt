import json
import queue
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .common import INFO, TranslationTask, sec2str, start_daemon_thread


class SSEBroadcaster:

    def __init__(self, host: str, port: int, path: str = '/events') -> None:
        self.host = host
        self.port = port
        self.path = path if path.startswith('/') else '/' + path
        self._lock = threading.Lock()
        self._subscribers = {}
        self._next_subscriber_id = 0
        self._next_event_id = 0
        self._closed = threading.Event()
        self._server = ThreadingHTTPServer((host, port), self._build_handler())
        self.bound_host, self.bound_port = self._server.server_address[:2]

    def _build_handler(self):
        broadcaster = self

        class SSEHandler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                if self.path == broadcaster.path:
                    broadcaster._handle_event_stream(self)
                    return
                if self.path == '/healthz':
                    payload = {
                        'status': 'ok',
                        'clients': broadcaster.client_count,
                        'path': broadcaster.path,
                    }
                    body = json.dumps(payload).encode('utf-8')
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, format, *args):
                return

        return SSEHandler

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def start(self):
        start_daemon_thread(self._server.serve_forever, poll_interval=0.5)
        print(f'{INFO}SSE server listening at http://{self.bound_host}:{self.bound_port}{self.path}')

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        self._server.shutdown()
        self._server.server_close()
        with self._lock:
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
        for subscriber_queue in subscribers:
            self._put_message(subscriber_queue, None)

    def publish_event(self, event: str, payload: dict):
        message = self._format_sse_message(event=event, data=payload)
        with self._lock:
            subscribers = list(self._subscribers.values())
        for subscriber_queue in subscribers:
            self._put_message(subscriber_queue, message)

    def publish_lifecycle(self, status: str, message: Optional[str] = None):
        payload = {
            'status': status,
            'message': message,
            'timestamp': self._utcnow(),
        }
        self.publish_event('lifecycle', payload)

    def publish_result(self, task: TranslationTask, output_whisper_result: bool, output_timestamps: bool):
        payload = {
            'task_id': task.task_id,
            'output_stage': task.output_stage,
            'timestamp': self._utcnow(),
            'time_range': {
                'start': task.time_range[0],
                'end': task.time_range[1],
                'start_text': sec2str(task.time_range[0]),
                'end_text': sec2str(task.time_range[1]),
            },
            'transcript': task.transcript,
            'translation': task.translation,
            'translation_failed': task.translation_failed,
            'output_whisper_result': output_whisper_result,
            'output_timestamps': output_timestamps,
            'display_text': self._build_display_text(task, output_whisper_result, output_timestamps),
        }
        self.publish_event('result', payload)

    def _handle_event_stream(self, handler: BaseHTTPRequestHandler):
        subscriber_queue = queue.Queue(maxsize=100)
        subscriber_id = self._register_subscriber(subscriber_queue)

        try:
            handler.send_response(HTTPStatus.OK)
            handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            handler.send_header('Cache-Control', 'no-cache, no-transform')
            handler.send_header('Connection', 'keep-alive')
            handler.send_header('Access-Control-Allow-Origin', '*')
            handler.end_headers()
            handler.wfile.write(b': connected\n\n')
            handler.wfile.flush()

            while not self._closed.is_set():
                try:
                    message = subscriber_queue.get(timeout=15)
                except queue.Empty:
                    handler.wfile.write(b': keep-alive\n\n')
                    handler.wfile.flush()
                    continue

                if message is None:
                    break
                handler.wfile.write(message)
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            self._unregister_subscriber(subscriber_id)

    def _register_subscriber(self, subscriber_queue: queue.Queue) -> int:
        with self._lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscribers[subscriber_id] = subscriber_queue
        return subscriber_id

    def _unregister_subscriber(self, subscriber_id: int):
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def _format_sse_message(self, event: str, data: dict) -> bytes:
        with self._lock:
            event_id = self._next_event_id
            self._next_event_id += 1
        encoded_data = json.dumps(data, ensure_ascii=False)
        lines = [f'id: {event_id}', f'event: {event}']
        lines.extend(f'data: {line}' for line in encoded_data.splitlines() or ['{}'])
        return ('\n'.join(lines) + '\n\n').encode('utf-8')

    def _put_message(self, subscriber_queue: queue.Queue, message: Optional[bytes]):
        try:
            subscriber_queue.put_nowait(message)
        except queue.Full:
            try:
                subscriber_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                subscriber_queue.put_nowait(message)
            except queue.Full:
                pass

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    @staticmethod
    def _build_display_text(task: TranslationTask, output_whisper_result: bool, output_timestamps: bool) -> str:
        chunks = []
        if task.output_stage == 'transcript':
            if output_timestamps:
                chunks.append(f'{sec2str(task.time_range[0])} --> {sec2str(task.time_range[1])}')
            if output_whisper_result and task.transcript:
                chunks.append(task.transcript)
        elif task.output_stage == 'translation':
            if output_timestamps:
                chunks.append(f'{sec2str(task.time_range[0])} --> {sec2str(task.time_range[1])}')
            if task.translation:
                chunks.append(task.translation)
        else:
            if output_timestamps:
                chunks.append(f'{sec2str(task.time_range[0])} --> {sec2str(task.time_range[1])}')
            if output_whisper_result and task.transcript:
                chunks.append(task.transcript)
            if task.translation:
                chunks.append(task.translation)
        return '\n'.join(chunks).strip()
