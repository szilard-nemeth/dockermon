import abc


class Notifyable:
    def __init__(self):
        pass

    @abc.abstractmethod
    def container_started(self, container_name, event_details):
        pass

    @abc.abstractmethod
    def container_became_healthy(self, container_name, event_details):
        pass

    @abc.abstractmethod
    def container_stopped_by_hand(self, container_name, event_details):
        pass

    @abc.abstractmethod
    def container_dead(self, container_name, event_details):
        pass

    @abc.abstractmethod
    def container_became_unhealthy(self, container_name, event_details):
        pass
