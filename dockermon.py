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
from restartservice import RestartService, DateHelper, RestartParameters

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


class DockerEvent:
    def __init__(self, event_type, container_name, timestamp):
        self.type = event_type
        self.container_name = container_name
        self.time = timestamp
        self.formatted_time = DateHelper.format_timestamp(timestamp)

    def __str__(self):
        return "type: %s, container_name: %s, time: %s, formatted_time: %s" \
               % (self.type, self.container_name, self.time, self.formatted_time)


class DockerMon:
    event_types_to_watch = ['die', 'stop', 'kill', 'start']

    def __init__(self, notification_service, restart_service):
        self.notification_service = notification_service
        self.restart_service = restart_service

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

    def watch(self, callback, url=default_sock_url, restart_callback=None):
        """Watch docker events. Will call callback with each new event (dict).

            url can be either tcp://<host>:port or ipc://<path>
        """
        sock, hostname = DockerMon.connect(url)
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

                parsed_json = json.loads(payload)
                if callback:
                    callback(parsed_json)

                if restart_callback:
                    self.restart_service.handle_docker_event(parsed_json)

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

    @staticmethod
    def event_max_age_in_seconds(ev, max_age_in_seconds, now):
        age_in_seconds = now - ev.time
        if age_in_seconds <= max_age_in_seconds:
            return True

    @staticmethod
    def event_type_matches(ev, event_type):
        if ev.type == event_type:
            return True

    @staticmethod
    def event_type_matches_one_of(ev, event_types):
        for ev_type in event_types:
            if ev.type == ev_type:
                return True
        return False


if __name__ == '__main__':
    import yaml
    import subprocess

    def setup_logging(
            default_path='logging.yaml',
            default_level=logging.INFO,
            env_key='LOG_CFG'):
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

    setup_logging()
    interpolate_script_filename = "/interpolate-env-vars.sh"
    if os.path.isfile(interpolate_script_filename):
        subprocess.check_call(interpolate_script_filename)

    arg_handler = ArgumentHandler()
    args = arg_handler.get_args()
    notification_service = notificationservice.NotificationService(args)

    if args.version:
        logger.info('dockermon %s', __version__)
        raise SystemExit

    if args.do_not_print_events:
        callback = None
    elif args.prog:
        prog = shlex.split(args.prog)
        callback = partial(DockerMon.prog_callback, prog)
    else:
        callback = DockerMon.print_callback

    restart_params = RestartParameters(args)

    restart_service = RestartService(args.socket_url, args.containers_to_restart, notification_service)
    dockermon = DockerMon(notification_service, restart_service)
    try:
        if args.restart_containers_on_die:
            dockermon.watch(callback, args.socket_url, restart_callback=restart_service.restart_callback)
        else:
            dockermon.watch(callback, args.socket_url)
    except (KeyboardInterrupt, EOFError):
        pass
