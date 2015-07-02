#!/usr/bin/env python
import gevent.monkey; gevent.monkey.patch_all(subprocess=True)
from flask import Flask, request, jsonify
from flask.ext.cors import CORS
import atexit
import gevent
import collections
from gevent import pywsgi
from geventwebsocket.handler import WebSocketHandler
import geventwebsocket
import ssl
import websocket
import json
import signal
import sys
import traceback
import os.path
import settings
import ycm_helpers

app = Flask(__name__)

cors = CORS(app, headers=["X-Requested-With", "X-CSRFToken", "Content-Type"], resources="/ycm/*")
mapping = {}


@app.route('/spinup', methods=['POST'])
def spinup():
    content = request.get_json(force=True)
    result = ycm_helpers.spinup(content)
    result['ws_port'] = settings.PORT
    result['secure'] = (settings.SSL_ROOT is not None)
    return jsonify(result)


def server_ws(process_uuid):
    ws_commands = {
        'completions': ycm_helpers.get_completions,
        'errors': ycm_helpers.get_errors,
        'goto': ycm_helpers.go_to,
        'create': ycm_helpers.create_file,
        'delete': ycm_helpers.delete_file,
        'ping': ycm_helpers.ping
    }

    # Get the WebSocket from the request context
    socket = request.environ.get('wsgi.websocket', None)
    if socket is None:
        return "websocket endpoint", 400

    # Functions to send back a response to a message, with its message ID.
    def send_response(message_id, response):
        if not isinstance(response, collections.Mapping):
            response = dict(message=response)
        response['_ws_message_id'] = message_id
        return socket.send(json.dumps(response))

    def send_error(message_id, message):
        response = (dict(success=False, error=message))
        send_response(message_id, response)

    # Loop for as long as the WebSocket remains open
    try:
        while True:
            raw = socket.receive()
            packet_id = None
            if raw is None:
                continue

            try:
                packet = json.loads(raw)
                packet_id = packet['_ws_message_id']
                command = packet['command']
                data = packet['data']
            except (KeyError, ValueError):
                send_error(packet_id, 'invalid packet')
                continue

            if command not in ws_commands:
                send_error(packet_id, 'unknown command')
                continue

            # Run the specified command with the correct uuid and data
            try:
                print "Running command: %s" % command
                result = ws_commands[command](process_uuid, data)
            except ycm_helpers.YCMProxyException as e:
                send_error(packet_id, e.message)
                continue
            except Exception as e:
                traceback.print_exc()
                send_error(packet_id, e.message)
                continue

            send_response(packet_id, result)
    except (websocket.WebSocketException, geventwebsocket.WebSocketError, TypeError):
        # WebSocket closed
        pass

    return ''


@app.route('/ycm/<process_uuid>/ws')
def ycm_ws(process_uuid):
    return server_ws(process_uuid)


@atexit.register
def kill_completers():
    print "Shutting down completers"
    ycm_helpers.kill_completers()


g = gevent.spawn(ycm_helpers.monitor_processes, mapping)
atexit.register(lambda: g.kill())


def run_server():
    app.debug = settings.DEBUG

    ssl_args = {}
    if settings.SSL_ROOT is not None:
        print "Running with SSL"
        ssl_args = {
            'keyfile': os.path.join(settings.SSL_ROOT, 'server-key.pem'),
            'certfile': os.path.join(settings.SSL_ROOT, 'server-cert.pem'),
            'ca_certs': os.path.join(settings.SSL_ROOT, 'ca-cert.pem'),
            'ssl_version': ssl.PROTOCOL_TLSv1,
        }
    server = pywsgi.WSGIServer(('', settings.PORT), app, handler_class=WebSocketHandler, **ssl_args)

    # Ensure that the program actually quits when we ask it to
    def sigterm_handler(_signo, _stack_frame):
        sys.exit(0)
    signal.signal(signal.SIGTERM, sigterm_handler)

    server.start()
    server.serve_forever()


if __name__ == '__main__':
    run_server()
