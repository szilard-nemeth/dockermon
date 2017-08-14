from contextlib import closing
import json
import logging
import time
import datetime
from dockermon import DockerMon, DockermonError, DockerEvent
from sys import version_info

if version_info[:2] < (3, 0):
    from httplib import OK as HTTP_OK
    from httplib import NO_CONTENT as HTTP_NO_CONTENT
    from urlparse import urlparse
else:
    from http.client import OK as HTTP_OK
    from urllib.parse import urlparse

restart_logger = logging.getLogger('dockermon-restart')


class RestartParameters:
    def __init__(self, args):
        self.threshold = args.restart_threshold
        self.limit = args.restart_limit
        self.reset_period = args.restart_reset_period
        self.containers_to_restart = args.containers_to_restart

class RestartData:
    def __init__(self, container_name, timestamp=None):
        self.container_name = container_name
        self.mail_sent = False
        if timestamp:
            self.occasions = [timestamp]
            self.formatted_occasions = [DateHelper.format_timestamp(timestamp)]
        else:
            self.occasions = []
            self.formatted_occasions = []

    def add_restart_occasion(self, timestamp):
        self.occasions.append(timestamp)
        self.formatted_occasions.append(DateHelper.format_timestamp(timestamp))

    def __str__(self):
        return "container_name: %s, occasions: %s, formatted_occasions: %s" \
               % (self.container_name, self.occasions, self.formatted_occasions)


class DateHelper:
    def __init__(self):
        pass

    @staticmethod
    def format_timestamp(timestamp):
        return datetime.datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')


class RestartService:
    def __init__(self, socket_url, restart_params, notification_service):
        self.socket_url = socket_url
        self.params = restart_params
        self.notification_service = notification_service

        self.captured_events = {}
        self.cached_container_names = {'restart': [], 'do_not_restart': []}
        self.restarts = {}

    @staticmethod
    def create_docker_restart_request(container_id, hostname):
        request = 'POST /containers/{0}/restart?t=5 HTTP/1.1\nHost: {1}\n\n'.format(container_id, hostname)
        request = request.encode('utf-8')
        return request

    def save_docker_event(self, event):
        container_name = event.container_name
        if container_name not in self.captured_events:
            self.captured_events[container_name] = []

        self.captured_events[container_name].append(event)

    def handle_docker_event(self, parsed_json):
        if "status" in parsed_json:
            event_status = parsed_json['status']
            container_name = parsed_json['Actor']['Attributes']["name"]
            event_time = parsed_json['time']

            if event_status in DockerMon.event_types_to_watch:
                docker_event = DockerEvent(event_status, container_name, event_time)
                self.save_docker_event(docker_event)
                if event_status == 'start':
                    self.maintain_container_restarts(container_name)
                elif self.check_container_is_restartable(container_name) and \
                        self.check_restart_needed(container_name, parsed_json):
                    restart_logger.info("Container %s dead unexpectedly, restarting...", container_name)
                    self.do_restart(parsed_json)

            # restart immediately if container is unhealthy
            elif "health_status: unhealthy" in event_status:
                docker_event = DockerEvent(event_status, container_name, event_time)
                self.save_docker_event(docker_event)
                if self.check_container_is_restartable(container_name) \
                        and self.check_restart_needed(container_name, parsed_json):
                    restart_logger.info("Container %s became unhealthy, restarting...", container_name)
                    self.maintain_container_restarts(container_name)
                    self.do_restart(parsed_json)

    def check_restart_needed(self, container_name, parsed_json):
        docker_events = self.captured_events[container_name]

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
                RestartService.log_stopped_killed_by_hand(container_name)
                return False
            else:
                if self.is_restart_allowed(container_name):
                    return True
                else:
                    RestartService.log_will_not_restart(container_name)
                    if not self.is_mail_sent(container_name):
                        subject = "Maximum restart count is reached for container %s" % container_name
                        self.notification_service.send_mail(subject, json.dumps(parsed_json))
                        self.set_mail_sent(container_name)
        else:
            return False

    @staticmethod
    def log_stopped_killed_by_hand(container_name):
        restart_logger.debug(
            "Container %s is stopped/killed, but WILL NOT BE restarted as it was stopped/killed by hand",
            container_name)

    def log_will_not_restart(self, container_name):
        restart_logger.warn(
            "Container %s is stopped/killed, but WILL NOT BE restarted again, as maximum restart count is reached: %s",
            container_name, self.params.restart_limit)

    def log_restart_container(self, container_name):
        count_of_restarts = self.get_performed_restart_count(container_name)
        log_record = "Restarting container: %s (%s / %s)..." % (
            container_name, count_of_restarts, self.params.restart_limit)
        restart_logger.info(log_record)
        return log_record

    def is_mail_sent(self, container_name):
        return self.restarts[container_name].mail_sent

    def set_mail_sent(self, container_name):
        self.restarts[container_name].mail_sent = True

    def save_restart_occasion(self, container_name):
        now = time.time()
        if container_name not in self.restarts:
            self.restarts[container_name] = RestartData(container_name, now)
        else:
            self.restarts[container_name].add_restart_occasion(now)

    def get_performed_restart_count(self, container_name):
        if container_name not in self.restarts:
            self.restarts[container_name] = RestartData(container_name)

        return len(self.restarts[container_name].occasions)

    def reset_restart_data(self, container_name):
        self.restarts[container_name] = RestartData(container_name)

    def check_container_is_restartable(self, container_name):
        if container_name in self.cached_container_names['restart']:
            return True
        elif container_name in self.cached_container_names['do_not_restart']:
            restart_logger.debug("Container %s is stopped/killed, "
                                 "but WILL NOT BE restarted as it does not match any names from configuration "
                                 "'containers-to-restart'.", container_name)
            return False
        else:
            for pattern in self.params.containers_to_restart:
                if pattern.match(container_name):
                    restart_logger.debug("Container %s is matched for container name pattern %s", container_name,
                                         pattern.pattern)
                    self.cached_container_names['restart'].append(container_name)
                    return True
            restart_logger.debug("Container %s is stopped/killed, "
                                 "but WILL NOT BE restarted as it does not match any names from configuration "
                                 "'containers-to-restart'.", container_name)
            self.cached_container_names['do_not_restart'].append(container_name)
            return False

    def is_restart_allowed(self, container_name):
        restart_count = self.get_performed_restart_count(container_name)
        last_restarts = self.restarts[container_name].occasions[-self.params.restart_limit:]

        now = time.time()
        restart_range_start = now - self.params.restart_threshold * 60
        for r in last_restarts:
            if r < restart_range_start:
                return False

        return restart_count < self.params.restart_limit

    def maintain_container_restarts(self, container_name):
        if container_name not in self.restarts:
            return
        last_restart = self.restarts[container_name].occasions[-1]
        now = time.time()
        restart_duration = now - last_restart
        needs_counter_reset = restart_duration < self.params.restart_reset_period * 60

        if needs_counter_reset:
            restart_logger.info("Start/healthy event received for container %s, clearing restart counter...",
                                container_name)
            restart_logger.info("Last restart time was %s", DateHelper.format_timestamp(last_restart))
            self.reset_restart_data(container_name)

    def do_restart(self, parsed_json):
        container_id = parsed_json['id']
        container_name = parsed_json['Actor']['Attributes']["name"]
        compose_service_name = parsed_json['Actor']['Attributes']["com.docker.compose.service"]

        sock, hostname = DockerMon.connect(self.socket_url)
        restart_logger.info("Sending restart request to Docker API for container: %s (%s), compose service name: %s",
                            container_name, container_id, compose_service_name)
        request = self.create_docker_restart_request(container_id, hostname)
        self.handle_restart_request(request, sock, container_name, json.dumps(parsed_json))

    def handle_restart_request(self, request, sock, container_name, mail_content):
        with closing(sock):
            sock.sendall(request)
            header, payload = DockerMon.read_http_header(sock)
            status, reason = DockerMon.header_status(header)

            # checking the HTTP status, no payload should be received!
            if status == HTTP_NO_CONTENT:
                self.save_restart_occasion(container_name)
                log_record = self.log_restart_container(container_name)
                self.notification_service.send_mail(log_record, mail_content)
            else:
                raise DockermonError('bad HTTP status: %s %s' % (status, reason))