import logging
import os
import smtplib
from email.mime.text import MIMEText
import socket

logger = logging.getLogger('notification')
restart_logger = logging.getLogger('dockermon-restart')


class NotificationService:

    def __init__(self, args):
        self.mail_recipients = NotificationService.get_mail_recipients(args)
        self.mail_hostname = NotificationService.get_mail_hostname()
        self.email_smtp_server = NotificationService.get_mail_server_address(args)

    def send_mail(self, subject, msg):
            if not self.mail_recipients:
                logger.warn('Skipping email notification as recipient email addresses are not set!')
                return

            email_msg = MIMEText(str(msg))
            email_from = 'dockermon'
            email_to = ', '.join(self.mail_recipients)
            email_subject = '%s: %s' % (self.mail_hostname, subject)

            email_msg['From'] = email_from
            email_msg['To'] = email_to
            email_msg['Subject'] = email_subject
            smtp = smtplib.SMTP(self.email_smtp_server)
            restart_logger.info('Sending mail to email addresses %s...', email_to)
            smtp.sendmail(email_from, self.mail_recipients, email_msg.as_string())
            smtp.quit()

    @staticmethod
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

    @staticmethod
    def get_mail_hostname():
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
        return mail_hostname

    @staticmethod
    def get_mail_server_address(args):
        return args.restart_notification_email_server if args.restart_notification_email_server else socket.gethostname()