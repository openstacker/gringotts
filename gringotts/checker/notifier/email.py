from gringotts import notifier as gring_notifier

from gringotts.checker import notifier
from gringotts.openstack.common import log
from gringotts.openstack.common.gettextutils import _


LOG = log.getLogger(__name__)


class EmailNotifier(notifier.Notifier):

    @staticmethod
    def notify_has_owed(context, account, contact, orders):
        subject = _('Hello, %s, you have %s resources have been owed') \
                % (contact['name'], len(orders))
        payload = {
            'actions': {
                'email': {
                    'template': 'account_has_owed',
                    'context': {
                        'subject': subject,
                        'orders': orders,
                        'reserved_days': account['reserved_days']
                    },
                    'from': 'noreply@unitedstack.com',
                    'to': contact['email']
                }
            }
        }
        notify = gring_notifier.get_notifier(service='checker')
        notify.info(context, 'uos.account.owed', payload)


    @staticmethod
    def notify_before_owed(context, account, contact, price_per_day, days_to_owe):
        subject = _('Hello, %s, your balance is insufficient') % contact['name']
        payload = {
            'actions': {
                'email': {
                    'template': 'account_will_owe',
                    'context': {
                        'subject': subject,
                        'price_per_day': price_per_day,
                        'balance': str(account['balance']),
                        'days_to_owe': days_to_owe
                    },
                    'from': 'noreply@unitedstack.com',
                    'to': contact['email']
                }
            }
        }
        notify = gring_notifier.get_notifier(service='checker')
        notify.info(context, 'uos.account.owed', payload)