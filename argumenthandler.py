import logging
import re

__author__ = 'szyszy'
import argparse
import pprint
import yaml

logger = logging.getLogger(__name__)
default_sock_url = 'ipc:///var/run/docker.sock'


class ArgumentHandler:
    def __init__(self):
        parser = self.create_parser()
        self.args = ArgumentHandler.parse_args(parser)

    def get_args(self):
        return self.args

    @staticmethod
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
        parser.add_argument('--print-all-events', default=True, action='store_true',
                            help='Print docker events to console')
        parser.set_defaults(print_all_events=True)

        # restart containers on unhealthy state OR when they are dead
        # manual kill won't restart
        restart_group = parser.add_mutually_exclusive_group()
        restart_group.add_argument('--restart-containers-on-die', dest='restart_containers_on_die', action='store_true')
        parser.set_defaults(restart_containers_on_die=False)

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
        restart_options_group.add_argument('--containers-to-watch', default=None,
                                           dest='containers_to_watch',
                                           help='Watch / Restart only specified containers, defaults to all containers',
                                           action='store')

        notification_options_group = parser.add_argument_group('Notification options')
        notification_options_group.add_argument('--notification-email-addresses-path', default=None,
                                                dest='notification_email_addresses_path',
                                                help='Send mail notifications of container restarts to the addresses from the specified file',
                                                action='store')
        notification_options_group.add_argument('---notification-email-server', default=None,
                                                dest='notification_email_server',
                                                help='Mail server for container restart email notifications',
                                                action='store')

        return parser

    @staticmethod
    def parse_args(parser):
        args = parser.parse_args()
        args.containers_to_watch = []
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
        if not args.containers_to_watch:
            args.containers_to_watch = ['.*']
        else:
            args.containers_to_watch = ArgumentHandler.convert_containers_to_watch(args.containers_to_watch)

        if not args.notification_email_server:
            raise SystemExit('Container restart notifications email server is not defined, exiting...')
        logger.debug("Command line arguments after processing: %s", pprint.pformat(args))
        return args

    @staticmethod
    def convert_containers_to_watch(containers):
        result = []
        logger.info("Converting 'containers-to-watch' arguments to regex patterns: %s", containers)
        for container in containers:
            if '*' in container:
                container = container.replace('*', '.*')
            compiled_regex = re.compile(container)
            result.append(compiled_regex)

        return result