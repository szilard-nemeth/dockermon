from contextlib import closing
import json
import logging
import time

from datehelper import DateHelper
from dockermon import DockerMon, DockermonError
from sys import version_info

from notifyable import Notifyable

if version_info[:2] < (3, 0):
    from httplib import NO_CONTENT as HTTP_NO_CONTENT
else:
    from http.client import OK as HTTP_OK

logger = logging.getLogger(__name__)


class RestartParameters:
    def __init__(self, args):
        self.restart_threshold = args.restart_threshold
        self.restart_limit = args.restart_limit
        self.restart_reset_period = args.restart_reset_period
        self.containers_to_watch = args.containers_to_watch
        self.do_restart = args.restart_containers_on_die


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


class RestartService(Notifyable):
    def __init__(self, socket_url, restart_params, notification_service):
        Notifyable.__init__(self)
        self.socket_url = socket_url
        self.params = restart_params
        self.notification_service = notification_service
        self.cached_container_names = {'restart': [], 'do_not_restart': []}
        self.restarts = {}

    def container_started(self, container_name, event_details):
        self.maintain_container_restart_counter(container_name)

    def container_became_healthy(self, container_name, event_details):
        self.maintain_container_restart_counter(container_name)

    def container_stopped_by_hand(self, container_name, event_details):
        logger.debug("Container %s is stopped/killed, but WILL NOT BE restarted "
                     "as it was stopped/killed by hand", container_name)

    def container_dead(self, container_name, event_details):
        if self.is_restart_allowed(container_name) and self.check_container_is_restartable(container_name):
            if self.params.do_restart:
                logger.info("Container %s dead unexpectedly, restarting...", container_name)
                self.do_restart(event_details)
            else:
                logger.info("Container %s dead unexpectedly, skipping restart but sending mail, as per configuration!",
                            container_name)
                self.notification_service.send_mail("Container %s dead unexpectedly" % container_name,
                                                    json.dumps(event_details))
        else:
            logger.warn("Container %s is stopped/killed, but WILL NOT BE restarted again, "
                        "as maximum restart count is reached: %s", container_name, self.params.restart_limit)
            if not self.is_mail_sent(container_name):
                mail_subject = "Maximum restart count is reached for container %s" % container_name
                self.notification_service.send_mail(mail_subject, json.dumps(event_details))
                self.set_mail_sent(container_name)
            pass

    def container_became_unhealthy(self, container_name, event_details):
        if self.is_restart_allowed(container_name) and self.check_container_is_restartable(container_name):
            if self.params.do_restart:
                logger.info("Container %s became unhealthy, restarting...", container_name)
                self.do_restart(event_details)
            else:
                logger.info("Container %s became unhealthy, skipping restart but sending mail, as per configuration!",
                            container_name)
                self.notification_service.send_mail("Container %s became unhealthy" % container_name,
                                                    json.dumps(event_details))

    def log_restart_container(self, container_name):
        count_of_restarts = self.get_performed_restart_count(container_name)
        log_record = "Restarting container: %s (%s / %s)..." % (
            container_name, count_of_restarts, self.params.restart_limit)
        logger.info(log_record)
        return log_record

    def is_mail_sent(self, container_name):
        return self.restarts[container_name].mail_sent

    def set_mail_sent(self, container_name):
        self.restarts[container_name].mail_sent = True

    def save_restart_event_happened(self, container_name):
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
            logger.debug("Container %s is stopped/killed, "
                         "but WILL NOT BE restarted as it does not match any names from configuration "
                         "'containers-to-watch'.", container_name)
            return False
        else:
            for pattern in self.params.containers_to_watch:
                if pattern.match(container_name):
                    logger.debug("Container %s is matched for container name pattern %s", container_name,
                                 pattern.pattern)
                    self.cached_container_names['restart'].append(container_name)
                    return True
            logger.debug("Container %s is stopped/killed, "
                         "but WILL NOT BE restarted as it does not match any names from configuration "
                         "'containers-to-watch'.", container_name)
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

    def maintain_container_restart_counter(self, container_name):
        if container_name not in self.restarts:
            return
        last_restart = self.restarts[container_name].occasions[-1]
        now = time.time()
        restart_duration = now - last_restart
        needs_counter_reset = restart_duration < self.params.restart_reset_period * 60

        if needs_counter_reset:
            logger.info("Start/healthy event received for container %s, clearing restart counter...",
                        container_name)
            logger.info("Last restart time was %s", DateHelper.format_timestamp(last_restart))
            self.reset_restart_data(container_name)

    def do_restart(self, parsed_json):
        container_id = parsed_json['id']
        container_name = parsed_json['Actor']['Attributes']["name"]
        compose_service_name = parsed_json['Actor']['Attributes']["com.docker.compose.service"]

        sock, hostname = DockerMon.connect(self.socket_url)
        logger.info("Sending restart request to Docker API for container: %s (%s), compose service name: %s",
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
                self.save_restart_event_happened(container_name)
                log_record = self.log_restart_container(container_name)
                self.notification_service.send_mail(log_record, mail_content)
            else:
                raise DockermonError('bad HTTP status: %s %s' % (status, reason))

    @staticmethod
    def create_docker_restart_request(container_id, hostname):
        request = 'POST /containers/{0}/restart?t=5 HTTP/1.1\nHost: {1}\n\n'.format(container_id, hostname)
        request = request.encode('utf-8')
        return request
