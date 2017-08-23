import logging
import os
import smtplib
from email.mime.text import MIMEText
import socket

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, args):
        self.mail_recipient_addresses = NotificationService.get_mail_addresses(args)
        self.mail_hostname = NotificationService.get_mail_hostname()
        self.email_smtp_server = NotificationService.get_mail_server_address(args)

    def send_mail(self, subject, msg):
        if not self.mail_recipient_addresses:
            logger.warn('Skipping email notification as recipient email addresses are not set!')
            return

        email_msg = MIMEText(str(msg))
        email_from = 'dockermon'
        email_to = ', '.join(self.mail_recipient_addresses)
        email_subject = '%s: %s' % (self.mail_hostname, subject)

        email_msg['From'] = email_from
        email_msg['To'] = email_to
        email_msg['Subject'] = email_subject
        smtp = smtplib.SMTP(self.email_smtp_server)
        logger.info('Sending mail to email addresses %s...', email_to)
        smtp.sendmail(email_from, self.mail_recipient_addresses, email_msg.as_string())
        smtp.quit()

    @staticmethod
    def get_mail_addresses(args):
        email_recipient_addresses = args.notification_email_addresses
        if not email_recipient_addresses:
            raise SystemExit('Container restart notification email recipient addresses is not provided or it is empty, exiting...')
        else:
            logger.info("Container restart notifications will be sent to these addresses: %s", email_recipient_addresses)
            return email_recipient_addresses

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
        return args.notification_email_server if args.notification_email_server else socket.gethostname()