#!/usr/bin/env python
"""docker monitor using docker /events HTTP streaming API"""
import os
from contextlib import closing
from functools import partial
from socket import AF_UNIX
from subprocess import Popen, PIPE
from sys import version_info
import json
import shlex
import logging
import logging.config
import socket
from argumenthandler import ArgumentHandler
import notificationservice
from eventbroadcaster import EventBroadcaster

if version_info[:2] < (3, 0):
    from httplib import OK as HTTP_OK
    from urlparse import urlparse
else:
    from http.client import OK as HTTP_OK
    from urllib.parse import urlparse

__version__ = '0.2.2'
bufsize = 1024
default_sock_url = 'ipc:///var/run/docker.sock'

logger = logging.getLogger('dockermon')
restart_logger = logging.getLogger('dockermon-restart')


class DockermonError(Exception):
    pass


class DockerMon:
    def __init__(self, callbacks, url=default_sock_url):
        self.url = url
        self.callbacks = callbacks
        self.event_broadcaster = EventBroadcaster()

    def register_listener(self, listener):
        self.event_broadcaster.register(listener)
        pass

    @staticmethod
    def read_http_header(sock):
        """Read HTTP header from socket, return header and rest of data."""
        buf = []
        hdr_end = '\r\n\r\n'

        while True:
            buf.append(sock.recv(bufsize).decode('utf-8'))
            data = ''.join(buf)
            i = data.find(hdr_end)
            if i == -1:
                continue
            return data[:i], data[i + len(hdr_end):]

    @staticmethod
    def header_status(header):
        """Parse HTTP status line, return status (int) and reason."""
        status_line = header[:header.find('\r')]
        # 'HTTP/1.1 200 OK' -> (200, 'OK')
        fields = status_line.split(None, 2)
        return int(fields[1]), fields[2]

    @staticmethod
    def connect(url):
        """Connect to UNIX or TCP socket.

            url can be either tcp://<host>:port or ipc://<path>
        """
        url = urlparse(url)
        if url.scheme == 'tcp':
            sock = socket.socket()
            netloc = tuple(url.netloc.rsplit(':', 1))
            hostname = socket.gethostname()
        elif url.scheme == 'ipc':
            sock = socket.socket(AF_UNIX)
            netloc = url.path
            hostname = 'localhost'
        else:
            raise ValueError('unknown socket type: %s' % url.scheme)

        sock.connect(netloc)
        return sock, hostname

    def watch(self):
        """Watch docker events. Will call callback with each new event (dict).

            url can be either tcp://<host>:port or ipc://<path>
        """
        sock, hostname = DockerMon.connect(self.url)
        request = 'GET /events HTTP/1.1\nHost: %s\n\n' % hostname
        request = request.encode('utf-8')

        with closing(sock):
            sock.sendall(request)
            header, payload = DockerMon.read_http_header(sock)
            status, reason = DockerMon.header_status(header)
            if status != HTTP_OK:
                raise DockermonError('bad HTTP status: %s %s' % (status, reason))

            # Messages are \r\n<size in hex><JSON payload>\r\n
            buf = [payload]
            while True:
                chunk = sock.recv(bufsize)
                if not chunk:
                    raise EOFError('socket closed')
                buf.append(chunk.decode('utf-8'))
                data = ''.join(buf)
                i = data.find('\r\n')
                if i == -1:
                    continue

                size = int(data[:i], 16)
                start = i + 2  # Skip initial \r\n

                if len(data) < start + size + 2:
                    continue
                payload = data[start:start + size]
                event_details = json.loads(payload)
                self.event_broadcaster.broadcast_event(event_details)

                if callbacks:
                    for callback in callbacks:
                        callback(event_details)

                buf = [data[start + size + 2:]]  # Skip \r\n suffix

    @staticmethod
    def print_callback(msg):
        """Print callback, prints message as info log as JSON in one line."""
        logger.info(json.dumps(msg))

    @staticmethod
    def prog_callback(prog, msg):
        """Program callback, calls prog with message in stdin"""
        pipe = Popen(prog, stdin=PIPE)
        data = json.dumps(msg)
        pipe.stdin.write(data.encode('utf-8'))
        pipe.stdin.close()


if __name__ == '__main__':
    import yaml
    import subprocess
    from restartservice import RestartService, RestartParameters

    def setup_logging(default_path='logging.yaml', default_level=logging.INFO, env_key='LOG_CFG'):
        """Setup logging configuration

        """
        path = default_path
        value = os.getenv(env_key, None)
        if value:
            path = value
        if os.path.exists(path):
            with open(path, 'rt') as f:
                config = yaml.safe_load(f.read())
            logging.config.dictConfig(config)
        else:
            logging.basicConfig(level=default_level)

    def get_args():
        interpolate_script_filename = "/interpolate-env-vars.sh"
        if not os.path.isfile(interpolate_script_filename):
            pass
            #raise SystemExit('Cannot interpolate env vars in config.yml, file not found in %s' % interpolate_script_filename)
        else:
            subprocess.check_call(interpolate_script_filename)
        arg_handler = ArgumentHandler()
        return arg_handler.get_args()


    def get_callbacks(args):
        callbacks = []
        if args.print_all_events:
            callbacks.append(DockerMon.print_callback)
        if args.prog:
            prog = shlex.split(args.prog)
            callbacks.append(partial(DockerMon.prog_callback, prog))

        return callbacks


    setup_logging()
    args = get_args()

    if args.version:
        logger.info('dockermon %s', __version__)
        raise SystemExit

    notification_service = notificationservice.NotificationService(args)
    callbacks = get_callbacks(args)
    dockermon = DockerMon(callbacks, args.socket_url)
    restart_params = RestartParameters(args)
    restart_service = RestartService(args.socket_url, restart_params, notification_service)
    dockermon.register_listener(restart_service)
    try:
        dockermon.watch()
    except (KeyboardInterrupt, EOFError):
        pass
