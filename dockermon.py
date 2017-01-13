#!/usr/bin/env python
"""docker monitor using docker /events HTTP streaming API"""

from contextlib import closing
from functools import partial
from socket import socket, AF_UNIX
from subprocess import Popen, PIPE
from sys import stdout, version_info
import json
import shlex

if version_info[:2] < (3, 0):
    from httplib import OK as HTTP_OK
    from httplib import NO_CONTENT as HTTP_NO_CONTENT
    from urlparse import urlparse
else:
    from http.client import OK as HTTP_OK
    from urllib.parse import urlparse

__version__ = '0.2.2'
bufsize = 1024
default_sock_url = 'ipc:///var/run/docker.sock'


class DockermonError(Exception):
    pass


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


def header_status(header):
    """Parse HTTP status line, return status (int) and reason."""
    status_line = header[:header.find('\r')]
    # 'HTTP/1.1 200 OK' -> (200, 'OK')
    fields = status_line.split(None, 2)
    return int(fields[1]), fields[2]


def connect(url):
    """Connect to UNIX or TCP socket.

        url can be either tcp://<host>:port or ipc://<path>
    """
    url = urlparse(url)
    if url.scheme == 'tcp':
        sock = socket()
        netloc = tuple(url.netloc.rsplit(':', 1))
        hostname = socket.gethostname()
    elif url.scheme == 'ipc':
        sock = socket(AF_UNIX)
        netloc = url.path
        hostname = 'localhost'
    else:
        raise ValueError('unknown socket type: %s' % url.scheme)

    sock.connect(netloc)
    return sock, hostname


def watch(callback, url=default_sock_url, restart_callback=None):
    """Watch docker events. Will call callback with each new event (dict).

        url can be either tcp://<host>:port or ipc://<path>
    """
    sock, hostname = connect(url)
    request = 'GET /events HTTP/1.1\nHost: %s\n\n' % hostname
    request = request.encode('utf-8')

    with closing(sock):
        sock.sendall(request)
        header, payload = read_http_header(sock)
        status, reason = header_status(header)
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
            callback(parsed_json)

            if restart_callback:
                if "status" in parsed_json:
                    event_status = parsed_json['status']
                else:
                    event_status = None
                if event_status and ("health_status: unhealthy" in event_status or "die" in event_status):
                    print "%s seems dead or unhealthy" % parsed_json['Actor']['Attributes']["name"]
                    print "Event status: " + event_status
                    print "Parsed json: " + str(parsed_json)
                    restart_callback(url, parsed_json)

            buf = [data[start + size + 2:]]  # Skip \r\n suffix


def print_callback(msg):
    """Print callback, prints message to stdout as JSON in one line."""
    json.dump(msg, stdout)
    stdout.write('\n')
    stdout.flush()


def prog_callback(prog, msg):
    """Program callback, calls prog with message in stdin"""
    pipe = Popen(prog, stdin=PIPE)
    data = json.dumps(msg)
    pipe.stdin.write(data.encode('utf-8'))
    pipe.stdin.close()


def restart_callback(url, msg):
    container_id = msg['id']
    container_name = msg['Actor']['Attributes']["name"]
    compose_service_name = msg['Actor']['Attributes']["com.docker.compose.service"]

    sock, hostname = connect(url)
    print "Sending restart request to Docker API for container: {0} ({1}), compose service name: {2}" \
        .format(container_name, container_id, compose_service_name)
    request = 'POST /containers/{0}/restart?t=5 HTTP/1.1\nHost: {1}\n\n'.format(container_id, hostname)
    request = request.encode('utf-8')

    with closing(sock):
        sock.sendall(request)
        header, payload = read_http_header(sock)
        status, reason = header_status(header)

        # checking the HTTP status, no payload should be received!
        if status == HTTP_NO_CONTENT:
            print "Restarting %s..." % container_name
        else:
            raise DockermonError('bad HTTP status: %s %s' % (status, reason))


if __name__ == '__main__':
    from argparse import ArgumentParser

    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--prog', default=None,
                        help='program to call (e.g. "jq --unbuffered .")')
    parser.add_argument(
        '--socket-url', default=default_sock_url,
        help='socket url (ipc:///path/to/sock or tcp:///host:port)')
    parser.add_argument(
        '--version', help='print version and exit',
        action='store_true', default=False)

    # restart containers on unhealthy state OR when they are dead
    # manual kill won't restart
    parser.add_argument('--restart-containers', dest='restart_containers', action='store_true')
    parser.add_argument('--do-not-restart-containers', dest='restart_containers', action='store_false')
    parser.set_defaults(restart_containers=True)
    args = parser.parse_args()

    if args.version:
        print('dockermon %s' % __version__)
        raise SystemExit

    if args.prog:
        prog = shlex.split(args.prog)
        callback = partial(prog_callback, prog)
    else:
        callback = print_callback

    try:
        if args.restart_containers:
            watch(callback, args.socket_url, restart_callback=restart_callback)
        else:
            watch(callback, args.socket_url)
    except (KeyboardInterrupt, EOFError):
        pass
