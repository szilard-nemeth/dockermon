import logging

from datehelper import DateHelper

logger = logging.getLogger(__name__)


class DockerEvent:
    def __init__(self, details, event_type, event_time, container_id, container_name, service_name):
        self.details = details
        self.type = event_type
        self.container_id = container_id
        self.container_name = container_name
        self.time = event_time
        self.service_name = service_name

        self.formatted_time = DateHelper.format_timestamp(event_time)

    def __str__(self):
        return "type: %s, container_id: %s, container_name: %s, service_name: %s, time: %s" \
               % (self.type, self.container_id, self.container_name, self.service_name, self.formatted_time)

    @classmethod
    def from_dict(cls, data):
        if data.get('status'):
            event_type = data['status']
            container_id = data['id']
            event_time = data['time']
            container_name = data['Actor']['Attributes']['name']
            # key is different in compose vs swarm / stack
            if data['Actor']['Attributes'].get("com.docker.compose.service"):
                service_name = data['Actor']['Attributes']["com.docker.compose.service"]
            else:
                service_name = data['Actor']['Attributes']["com.docker.swarm.service.name"]
        else:
            # if we don't have status, it could be any other not container related event
            # e.g. network disconnect
            event_type = None
            container_id = None
            container_name = None
            service_name = None
            event_time = data['time']

        return cls(data, event_type, event_time, container_id, container_name, service_name)
