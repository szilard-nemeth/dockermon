import abc


class Notifyable:
    def __init__(self):
        pass

    @abc.abstractmethod
    def container_started(self, event):
        pass

    @abc.abstractmethod
    def container_became_healthy(self, event):
        pass

    @abc.abstractmethod
    def container_stopped_by_hand(self, event):
        pass

    @abc.abstractmethod
    def container_dead(self, event):
        pass

    @abc.abstractmethod
    def container_became_unhealthy(self, event):
        pass
