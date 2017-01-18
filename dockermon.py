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

import time
import datetime

import re
import logging
import logging.config
import smtplib
import socket
from email.mime.text import MIMEText

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

logger = logging.getLogger('dockermon')
restart_logger = logging.getLogger('dockermon-restart')


class DockermonError(Exception):
    pass


class Helper:
    def __init__(self):
        pass

    @staticmethod
    def format_timestamp(timestamp):
        return datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')


class DockerEvent:
    def __init__(self, event_type, container_name, timestamp):
        self.type = event_type
        self.container_name = container_name
        self.time = timestamp
        self.formatted_time = Helper.format_timestamp(timestamp)

    def __str__(self):
        return "type: %s, container_name: %s, time: %s, formatted_time: %s" \
               % (self.type, self.container_name, self.time, self.formatted_time)


class RestartData:
    def __init__(self, container_name, timestamp=None):
        self.container_name = container_name
        self.mail_sent = False
        if timestamp:
            self.occasions = [timestamp]
            self.formatted_occasions = [Helper.format_timestamp(timestamp)]
        else:
            self.occasions = []
            self.formatted_occasions = []

    def add_restart_occasion(self, timestamp):
        self.occasions.append(timestamp)
        self.formatted_occasions.append(Helper.format_timestamp(timestamp))

    def __str__(self):
        return "container_name: %s, occasions: %s, formatted_occasions: %s" \
               % (self.container_name, self.occasions, self.formatted_occasions)


class DockerMon:
    event_types_to_watch = ['die', 'stop', 'kill', 'start']

    def __init__(self, args, mail_recipients, mail_hostname):
        self.event_dict = {}
        self.container_restarts = {}
        self.args = args
        self.mail_recipients = mail_recipients
        self.mail_hostname = mail_hostname
        self.cached_container_names = {'restart': [], 'do_not_restart': []}

    def save_docker_event(self, event):
        container_name = event.container_name
        if container_name not in self.event_dict:
            self.event_dict[container_name] = []

        self.event_dict[container_name].append(event)

    def save_restart_occasion(self, container_name):
        now = time.time()
        if container_name not in self.container_restarts:
            self.container_restarts[container_name] = RestartData(container_name, now)
        else:
            self.container_restarts[container_name].add_restart_occasion(now)

    def get_performed_restart_count(self, container_name):
        if container_name not in self.container_restarts:
            self.container_restarts[container_name] = RestartData(container_name)

        return len(self.container_restarts[container_name].occasions)

    def reset_restart_data(self, container_name):
        self.container_restarts[container_name] = RestartData(container_name)

    def send_mail(self, subject, msg):
        if not self.mail_recipients:
            logger.warn('Skipping email notification as recipient email addresses are not set!')
            return

        email_smtp_server = self.args.restart_notification_email_server if self.args.restart_notification_email_server else socket.gethostname()

        email_msg = MIMEText(str(msg))
        email_from = 'dockermon'
        email_to = ', '.join(self.mail_recipients)
        email_subject = '%s: %s' % (self.mail_hostname, subject)

        email_msg['From'] = email_from
        email_msg['To'] = email_to
        email_msg['Subject'] = email_subject
        smtp = smtplib.SMTP(email_smtp_server)
        restart_logger.info('Sending mail to email addresses %s...', email_to)
        smtp.sendmail(email_from, self.mail_recipients, email_msg.as_string())
        smtp.quit()

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
                callback(parsed_json)

                if restart_callback:
                    if "status" in parsed_json:
                        event_status = parsed_json['status']
                        container_name = parsed_json['Actor']['Attributes']["name"]
                        event_time = parsed_json['time']

                        if event_status in DockerMon.event_types_to_watch:
                            docker_event = DockerEvent(event_status, container_name, event_time)
                            self.save_docker_event(docker_event)
                            if event_status == 'start':
                                self.maintain_container_restarts(container_name)
                            elif self.check_container_is_restartable(container_name) and self.check_restart_needed(
                                    container_name, parsed_json):
                                restart_logger.info("Container %s dead unexpectedly, restarting...", container_name)
                                restart_callback(url, parsed_json)

                        # restart immediately if container is unhealthy
                        elif "health_status: unhealthy" in event_status \
                                and self.check_container_is_restartable(container_name) \
                                and self.check_restart_needed(
                                container_name, parsed_json):
                            restart_logger.info("Container %s became unhealthy, restarting...", container_name)
                            docker_event = DockerEvent(event_status, container_name, event_time)
                            self.save_docker_event(docker_event)
                            self.maintain_container_restarts(container_name)
                            restart_callback(url, parsed_json)

                buf = [data[start + size + 2:]]  # Skip \r\n suffix

    def check_container_is_restartable(self, container_name):
        if container_name in self.cached_container_names['restart']:
            return True
        elif container_name in self.cached_container_names['do_not_restart']:
            restart_logger.debug("Container %s is stopped/killed, "
                                 "but WILL NOT BE restarted as it does not match any names from configuration "
                                 "'containers-to-restart'.", container_name)
            return False
        else:
            for pattern in self.args.containers_to_restart:
                if pattern.match(container_name):
                    restart_logger.debug("Container %s is matched for container name pattern %s", container_name, pattern.pattern)
                    self.cached_container_names['restart'].append(container_name)
                    return True
            restart_logger.debug("Container %s is stopped/killed, "
                                 "but WILL NOT BE restarted as it does not match any names from configuration "
                                 "'containers-to-restart'.", container_name)
            self.cached_container_names['do_not_restart'].append(container_name)
            return False

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

    def check_restart_needed(self, container_name, parsed_json):
        docker_events = self.event_dict[container_name]

        if not docker_events:
            return False

        now = time.time()
        die_events = filter(lambda e: DockerMon.event_type_matches(e, 'die'), docker_events)
        die_events = filter(lambda e: DockerMon.event_max_age_in_seconds(e, 5, now), die_events)

        if die_events:
            stop_or_kill_events = filter(lambda e: DockerMon.event_type_matches_one_of(e, ['stop', 'kill']),
                                         docker_events)
            stop_or_kill_events = filter(lambda e: DockerMon.event_max_age_in_seconds(e, 12, now), stop_or_kill_events)

            if stop_or_kill_events:
                restart_logger.debug(
                    "Container %s is stopped/killed, but WILL NOT BE restarted as it was stopped/killed by hand",
                    container_name)
                return False
            else:
                if self.is_restart_allowed(container_name):
                    return True
                else:
                    restart_logger.warn(
                        "Container %s is stopped/killed, but WILL NOT BE restarted again, as maximum restart count is reached: %s",
                        container_name, self.args.restart_limit)

                    if not self.container_restarts[container_name].mail_sent:
                        subject = "Maximum restart count is reached for container %s" % container_name
                        self.send_mail(subject, json.dumps(parsed_json))
                        self.container_restarts[container_name].mail_sent = True
        else:
            return False

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

    def restart_callback(self, url, parsed_json):
        container_id = parsed_json['id']
        container_name = parsed_json['Actor']['Attributes']["name"]
        compose_service_name = parsed_json['Actor']['Attributes']["com.docker.compose.service"]

        sock, hostname = DockerMon.connect(url)
        restart_logger.info("Sending restart request to Docker API for container: %s (%s), compose service name: %s",
                            container_name, container_id, compose_service_name)
        request = 'POST /containers/{0}/restart?t=5 HTTP/1.1\nHost: {1}\n\n'.format(container_id, hostname)
        request = request.encode('utf-8')

        with closing(sock):
            sock.sendall(request)
            header, payload = DockerMon.read_http_header(sock)
            status, reason = DockerMon.header_status(header)

            # checking the HTTP status, no payload should be received!
            if status == HTTP_NO_CONTENT:
                self.save_restart_occasion(container_name)
                count_of_restarts = self.get_performed_restart_count(container_name)
                log_record = "Restarting container: %s (%s / %s)..." % (container_name, count_of_restarts, self.args.restart_limit)
                restart_logger.info(log_record)
                self.send_mail(log_record, json.dumps(parsed_json))
            else:
                raise DockermonError('bad HTTP status: %s %s' % (status, reason))

    def is_restart_allowed(self, container_name):
        restart_count = self.get_performed_restart_count(container_name)
        last_restarts = self.container_restarts[container_name].occasions[-self.args.restart_limit:]

        now = time.time()
        restart_range_start = now - self.args.restart_threshold * 60
        for r in last_restarts:
            if r < restart_range_start:
                return False

        return restart_count < self.args.restart_limit

    def maintain_container_restarts(self, container_name):
        if container_name not in self.container_restarts:
            return
        last_restart = self.container_restarts[container_name].occasions[-1]
        now = time.time()
        restart_reset_range_start = now - self.args.restart_reset_period * 60
        if last_restart < restart_reset_range_start:
            restart_logger.info("Start/healthy event received for container %s, clearing restart counter...", container_name)
            restart_logger.info("Last restart time was %s", Helper.format_timestamp(last_restart))
            self.reset_restart_data(container_name)


if __name__ == '__main__':
    import argparse
    import yaml
    import subprocess
    import pprint


    def setup_logging(
            default_path='logging.yaml',
            default_level=logging.INFO,
            env_key='LOG_CFG'
    ):
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
        parser = create_parser()
        return parse_args(parser)


    def create_parser():
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--prog', default=None,
                            help='program to call (e.g. "jq --unbuffered .")')
        parser.add_argument('--socket-url', default=default_sock_url,
                            help='socket url (ipc:///path/to/sock or tcp:///host:port)')
        parser.add_argument('--version', default=False,
                            help='print version and exit', action='store_true')

        parser.add_argument('--config-file',
                            dest='config_file',
                            help='config file in yaml format',
                            type=argparse.FileType(mode='r'))

        # restart containers on unhealthy state OR when they are dead
        # manual kill won't restart
        restart_group = parser.add_mutually_exclusive_group()
        restart_group.add_argument('--restart-containers-on-die', dest='restart_containers_on_die', action='store_true')
        restart_group.add_argument('--do-not-restart-containers-on-die', dest='restart_containers_on_die',
                                   action='store_false')
        parser.set_defaults(restart_containers_on_die=True)

        restart_options_group = parser.add_argument_group('Restart options')
        restart_options_group.add_argument('--restart-limit', default=3,
                                           dest='restart_limit',
                                           help='Consecutive restart allowed in restart threshold period',
                                           action='store')
        restart_options_group.add_argument('--restart-threshold', default=10,
                                           dest='restart_threshold',
                                           help='Period in minutes that limits consecutive restarts',
                                           action='store')
        restart_options_group.add_argument('--restart-reset-period', default=2,
                                           dest='restart_reset_period',
                                           help='Minutes to wait to reset restart counter for containers',
                                           action='store')
        # default=['*'] appended the given arguments to the default which is not the desired behavior
        restart_options_group.add_argument('--containers-to-restart', default=None,
                                           dest='containers_to_restart',
                                           help='Restart only specified containers, defaults to all containers',
                                           action='store')

        notification_options_group = parser.add_argument_group('Notification options')
        notification_options_group.add_argument('--restart-notification-email-addresses-path', default=None,
                                                dest='restart_notification_email_addresses_path',
                                                help='Send mail notifications of container restarts to the addresses from the specified file',
                                                action='store')
        notification_options_group.add_argument('--restart-notification-email-server', default=None,
                                                dest='restart_notification_email_server',
                                                help='Mail server for container restart email notifications',
                                                action='store')

        return parser


    def parse_args(parser):
        args = parser.parse_args()
        args.containers_to_restart = []
        if args.config_file:
            logger.info("Using config file %s", args.config_file.name)
            data = yaml.load(args.config_file)
            delattr(args, 'config_file')
            arg_dict = args.__dict__

            logger.debug("Values read from config file: %s", data.items())
            for key, value in data.items():
                key = key.replace('-', '_')
                if not value:
                    logger.warn("Omitting empty value from config file for key: %s!", key)
                    continue

                logger.debug("Using param from config file: %s=%s", key, value)
                if isinstance(value, list):
                    for v in value:
                        arg_dict[key].append(v)
                else:
                    arg_dict[key] = value

        # initialize list if empty
        if not args.containers_to_restart:
            args.containers_to_restart = ['.*']
        else:
            args.containers_to_restart = convert_containers_to_restart(args.containers_to_restart)

        if not args.restart_notification_email_server:
            raise SystemExit('Container restart notifications email server is not defined, exiting...')
        logger.debug("Command line arguments after processing: %s", pprint.pformat(args))
        return args


    def convert_containers_to_restart(containers_to_restart):
        result = []
        logger.info("Converting 'containers-to-restart' arguments to regex patterns: %s", containers_to_restart)
        for container in containers_to_restart:
            if '*' in container:
                container = container.replace('*', '.*')
            compiled_regex = re.compile(container)
            result.append(compiled_regex)

        return result

    def get_mail_recipients(args):
        recipient_list_file = args.restart_notification_email_addresses_path
        if not args.restart_notification_email_addresses_path:
            raise SystemExit('Container restart notifications email recipient list file path is not provided, exiting...')
        elif not os.path.exists(recipient_list_file):
            raise SystemExit('Container restart notifications email recipient list file %s is not found or not readable, exiting...' % recipient_list_file)
        else:
            if os.path.exists(recipient_list_file):
                with open(recipient_list_file) as f:
                    mail_recipients = f.read().splitlines()
                if not mail_recipients:
                    raise SystemExit('Container restart notifications email recipient list file %s seems empty, exiting...' % recipient_list_file)
                else:
                    logger.info("Container restart notifications will be sent to these addresses: %s", mail_recipients)
                    return mail_recipients

    setup_logging()
    interpolate_script_filename = "/interpolate-env-vars.sh"
    if os.path.isfile(interpolate_script_filename):
        subprocess.check_call(interpolate_script_filename)

    host_hostname_filename = '/dockermon/host-hostname'
    if os.path.isfile(host_hostname_filename):
        mail_hostname = open(host_hostname_filename).read().replace('\n', '')
        logger.debug("Provided hostname of host machine %s", mail_hostname)

    try:
        mail_hostname
    except NameError:
        mail_hostname = 'root'
    else:
        pass
    logger.debug("Hostname will be used for notification emails: %s", mail_hostname)

    args = get_args()

    mail_recipients = get_mail_recipients(args)

    if args.version:
        logger.info('dockermon %s', __version__)
        raise SystemExit

    if args.prog:
        prog = shlex.split(args.prog)
        callback = partial(DockerMon.prog_callback, prog)
    else:
        callback = DockerMon.print_callback

    dockermon = DockerMon(args, mail_recipients, mail_hostname)
    try:
        if args.restart_containers_on_die:
            dockermon.watch(callback, args.socket_url, restart_callback=dockermon.restart_callback)
        else:
            dockermon.watch(callback, args.socket_url)
    except (KeyboardInterrupt, EOFError):
        pass
