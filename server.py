#!/usr/bin/env python

import sys
import io
import os
import shutil
from subprocess import Popen, PIPE
from string import Template
from struct import Struct
from threading import Thread
from time import sleep, time
from http.server import BaseHTTPRequestHandler # Used only for date utility now
from wsgiref.simple_server import make_server

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput
from libcamera import Transform
from ws4py.websocket import WebSocket
from ws4py.server.wsgirefserver import (
    WSGIServer,
    WebSocketWSGIHandler,
    WebSocketWSGIRequestHandler,
)
from ws4py.server.wsgiutils import WebSocketWSGIApplication

###########################################
# CONFIGURATION
WIDTH = 640
HEIGHT = 480
FRAMERATE = 24
HTTP_PORT = 8082
WS_PORT = HTTP_PORT # **UNIFIED PORT:** Both services run on 8082
COLOR = u'#444'
BGCOLOR = u'#333'
JSMPEG_MAGIC = b'jsmp'
JSMPEG_HEADER = Struct('>4sHH')
VFLIP = False
HFLIP = False

# Global variables to store file contents and the WSGI WebSocket application
INDEX_TEMPLATE = None
JSMPEG_CONTENT = None
WSGI_WS_APP = None
###########################################

# --- WSGI HTTP Application (Replaces StreamingHttpHandler/Server) ---

# This function handles serving files (index.html, jsmpg.js) as a WSGI application
def http_app(environ, start_response):
    """
    Handles standard HTTP requests for the HTML and JavaScript files.
    """
    path = environ.get('PATH_INFO', '')
    
    if path == '/':
        start_response('301 Moved Permanently', [('Location', '/index.html')])
        return []
    elif path == '/jsmpg.js':
        content_type = 'application/javascript'
        content = JSMPEG_CONTENT
    elif path == '/index.html':
        content_type = 'text/html; charset=utf-8'
        tpl = Template(INDEX_TEMPLATE)
        # We no longer need to reference WS_PORT, as the client will use the same port as the page
        content = tpl.safe_substitute(dict(
            WS_PORT=WS_PORT, WIDTH=WIDTH, HEIGHT=HEIGHT, COLOR=COLOR,
            BGCOLOR=BGCOLOR))
    else:
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'File not found']
    
    content = content.encode('utf-8')
    
    # Simple headers for file serving
    start_response('200 OK', [
        ('Content-Type', content_type),
        ('Content-Length', str(len(content))),
        ('Last-Modified', BaseHTTPRequestHandler.date_time_string(time()))
    ])
    return [content]

# --- Combined WSGI Dispatcher Application ---

def application(environ, start_response):
    """
    Main WSGI application that dispatches requests.
    If 'Upgrade: websocket' header is present, it routes to the ws4py app.
    Otherwise, it routes to the standard HTTP file serving app.
    """
    # Check if this is a WebSocket upgrade request
    if environ.get('HTTP_UPGRADE', '').lower() == 'websocket':
        # Route to the ws4py handler
        return WSGI_WS_APP(environ, start_response)
    else:
        # Route to the standard HTTP file server
        return http_app(environ, start_response)

# --- WebSocket and Broadcast Classes (Unchanged, but integrated below) ---

class StreamingWebSocket(WebSocket):
    def opened(self):
        self.send(JSMPEG_HEADER.pack(JSMPEG_MAGIC, WIDTH, HEIGHT), binary=True)


class BroadcastOutput(object):
    def __init__(self, camera):
        print('Spawning background conversion process')
        resolution = camera.camera_config['main']['size']
        framerate = camera.camera_config['main']['format'].split('@')[1].rstrip('fps') if '@' in str(camera.camera_config['main']['format']) else str(FRAMERATE)
        self.converter = Popen([
            'ffmpeg',
            '-f', 'rawvideo',
            '-pix_fmt', 'yuv420p',
            '-s', '%dx%d' % resolution,
            '-r', framerate,
            '-i', '-',
            '-f', 'mpeg1video',
            '-b', '800k',
            '-r', framerate,
            '-'],
            stdin=PIPE, stdout=PIPE, stderr=io.open(os.devnull, 'wb'),
            shell=False, close_fds=True)

    def write(self, b):
        self.converter.stdin.write(b)

    def flush(self):
        print('Waiting for background conversion process to exit')
        self.converter.stdin.close()
        self.converter.wait()


class BroadcastThread(Thread):
    def __init__(self, converter, websocket_server):
        super(BroadcastThread, self).__init__()
        self.converter = converter
        self.websocket_server = websocket_server

    def run(self):
        try:
            while True:
                buf = self.converter.stdout.read1(32768)
                if buf:
                    # websocket_server is now the combined_server
                    self.websocket_server.manager.broadcast(buf, binary=True)
                elif self.converter.poll() is not None:
                    break
        finally:
            self.converter.stdout.close()


def main():
    global INDEX_TEMPLATE, JSMPEG_CONTENT, WSGI_WS_APP
    
    # Load content files once before starting the server
    try:
        with io.open('index.html', 'r') as f:
            INDEX_TEMPLATE = f.read()
        with io.open('jsmpg.js', 'r') as f:
            JSMPEG_CONTENT = f.read()
    except IOError as e:
        print("ERROR: Could not find 'index.html' or 'jsmpg.js'. Ensure these files are in the same directory.")
        print(e)
        return

    print('Initializing camera')
    camera = Picamera2()

    # Configure camera with transform for flips
    config = camera.create_video_configuration(
        main={"size": (WIDTH, HEIGHT), "format": "YUV420"},
        transform=Transform(hflip=HFLIP, vflip=VFLIP)
    )
    camera.configure(config)
    camera.start()
    sleep(1) # camera warm-up time

    try:
        # 1. Initialize the ws4py WebSocket application component
        WSGI_WS_APP = WebSocketWSGIApplication(handler_cls=StreamingWebSocket)

        print('Initializing unified HTTP/WS server on port %d' % HTTP_PORT)

        # 2. Create the combined server using the dispatching 'application' function
        WebSocketWSGIHandler.http_version = '1.1'
        combined_server = make_server(
            '', HTTP_PORT, # Uses the unified port 8082
            server_class=WSGIServer,
            handler_class=WebSocketWSGIRequestHandler,
            app=application # Uses the application() dispatcher
        )
        combined_server.initialize_websockets_manager()
        server_thread = Thread(target=combined_server.serve_forever)

        print('Initializing broadcast thread')
        output = BroadcastOutput(camera)
        broadcast_thread = BroadcastThread(output.converter, combined_server)
        print('Starting recording')
        camera.start_recording(output)

        try:
            print('Starting server thread (HTTP and WS)')
            server_thread.start()
            print('Starting broadcast thread')
            broadcast_thread.start()

            while True:
                sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            print('Stopping recording')
            camera.stop_recording()
            print('Waiting for broadcast thread to finish')
            broadcast_thread.join()
            print('Shutting down server')
            combined_server.shutdown()
            print('Waiting for server thread to finish')
            server_thread.join()
    finally:
        camera.stop()
        camera.close()


if __name__ == '__main__':
    main()

