import time

import logging

from dockerevent import DockerEvent
from notifyable import Notifyable

logger = logging.getLogger(__name__)


class EventBroadcaster:
    def __init__(self):
        self.events_to_watch = ['die', 'stop', 'kill', 'start', 'health_status: healthy', 'health_status: unhealthy']
        self.listeners = []
        self.captured_events = {}

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

    @staticmethod
    def event_max_age_in_seconds(ev, max_age_in_seconds, now):
        age_in_seconds = now - ev.time
        if age_in_seconds <= max_age_in_seconds:
            return True

    @staticmethod
    def log_propagate_event(event_type, container_name):
        logger.debug('Propagating %s event for container: %s' % (event_type, container_name))

    def register(self, listener):
        if not isinstance(listener, Notifyable):
            raise TypeError("listener must be of type Notifyable")
        self.listeners.append(listener)

    def save_docker_event(self, event):
        container_name = event.container_name
        if container_name not in self.captured_events:
            self.captured_events[container_name] = []

        self.captured_events[container_name].append(event)
        logger.debug('Saved docker event %s for container %s' % (event, container_name))

    def notify_container_started(self, docker_event):
        self.log_propagate_event('container-started', docker_event.container_name)
        for listener in self.listeners:
            listener.container_started(docker_event)

    def notify_container_became_healthy(self, docker_event):
        self.log_propagate_event('container-became-healthy', docker_event.container_name)
        for listener in self.listeners:
            listener.container_became_healthy(docker_event)

    def notify_container_stopped_by_hand(self, docker_event):
        self.log_propagate_event('container-stopped-by-hand', docker_event.container_name)
        for listener in self.listeners:
            listener.container_stopped_by_hand(docker_event)

    def notify_container_dead(self, docker_event):
        self.log_propagate_event('container-container-dead', docker_event.container_name)
        for listener in self.listeners:
            listener.container_dead(docker_event)

    def notify_container_became_unhealthy(self, docker_event):
        self.log_propagate_event('container-became-unhealthy', docker_event.container_name)
        for listener in self.listeners:
            listener.container_became_unhealthy(docker_event)

    def broadcast_event(self, event_details):
        if "status" in event_details:
            docker_event = DockerEvent.from_dict(event_details)
            container_name = docker_event.container_name

            if docker_event.type in self.events_to_watch:
                self.save_docker_event(docker_event)
                if docker_event.type == 'start':
                    self.notify_container_started(docker_event)
                elif docker_event.type == 'health_status: healthy':
                    self.notify_container_became_healthy(docker_event)
                elif docker_event.type == 'health_status: unhealthy':
                    if self.check_notify_required(container_name):
                        self.notify_container_became_unhealthy(docker_event)
                elif self.check_notify_required(container_name):
                    self.notify_container_dead(docker_event)

    def check_notify_required(self, container_name):
        docker_events = self.captured_events[container_name]
        if not docker_events:
            logger.debug('Skipped event propagation, container %s does not have saved events' % container_name)
            return False

        die_events = self.get_die_events_from_last_period(container_name)
        if not die_events:
            logger.debug(
                'Skipped event propagation, container %s does not have \'die\' events from the last period' % container_name)
            return False

        stop_or_kill_events = self.get_stop_or_kill_events_from_last_period(container_name)
        if stop_or_kill_events:
            logger.debug(
                'Skipped event propagation, container %s does not have \'stop\' / \'kill\' events from the last period' % container_name)
            return False
        else:
            return True

    def get_die_events_from_last_period(self, container_name):
        docker_events = self.captured_events[container_name]
        now = time.time()
        die_events = filter(lambda e: EventBroadcaster.event_type_matches(e, 'die'), docker_events)
        die_events = filter(lambda e: EventBroadcaster.event_max_age_in_seconds(e, 5, now), die_events)
        return die_events

    def get_stop_or_kill_events_from_last_period(self, container_name):
        docker_events = self.captured_events[container_name]
        now = time.time()
        stop_or_kill_events = filter(lambda e: EventBroadcaster.event_type_matches_one_of(e, ['stop', 'kill']),
                                     docker_events)
        stop_or_kill_events = filter(lambda e: EventBroadcaster.event_max_age_in_seconds(e, 12, now),
                                     stop_or_kill_events)
        return stop_or_kill_events
