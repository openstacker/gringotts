import pecan
import wsme
import datetime

from pecan import rest
from pecan import request
from wsmeext.pecan import wsexpose
from wsme import types as wtypes

from oslo_config import cfg

from gringotts import master
from gringotts import exception
from gringotts import utils as gringutils
from gringotts.api.v1 import models
from gringotts.db import models as db_models
from gringotts.services import keystone
from gringotts.checker import notifier
from gringotts.openstack.common import log
from gringotts.openstack.common import uuidutils
from gringotts.openstack.common import timeutils

cfg.CONF.import_opt('notifier_level', 'gringotts.checker.service', group='checker')
LOG = log.getLogger(__name__)


class AccountController(rest.RestController):
    """Manages operations on account
    """

    _custom_actions = {
        'level': ['PUT'],
        'charges': ['GET'],
        'estimate': ['GET'],
        'estimate_per_day': ['GET'],
    }

    def __init__(self, project_id):
        pecan.request.context['project_id'] = project_id
        self._id = project_id

    def _account(self):
        self.conn = pecan.request.db_conn
        try:
            account = self.conn.get_account(request.context,
                                            None,
                                            project_id=self._id)
        except Exception as e:
            LOG.error('account %s not found' % self._id)
            raise exception.AccountByProjectNotFound(project_id=self._id)
        return account

    @wsexpose(models.UserAccount, int)
    def level(self, level):
        if not isinstance(level, int) or level < 0 or level > 9:
            raise exception.InvalidParameterValue(err="Invalid Level")

        self.conn = pecan.request.db_conn
        try:
            account = self.conn.change_account_level(request.context,
                                                     None,
                                                     level,
                                                     project_id=self._id)
        except Exception as e:
            LOG.exception('Fail to change the account level of: %s' % self._id)
            raise exception.DBError(reason=e)

        return models.UserAccount.from_db_model(account)

    @wsexpose(models.UserAccount)
    def get(self):
        """Return this product"""
        return models.UserAccount.from_db_model(self._account())

    @wsexpose(models.Charge, wtypes.text, body=models.Charge)
    def put(self, data):
        """Charge the account
        """
        # Check the charge value
        if data.value < -100000 or data.value > 100000:
            raise exception.InvalidChargeValue(value=data.value)

        self.conn = pecan.request.db_conn

        try:
            charge, _ = self.conn.update_account(request.context,
                                                 None,
                                                 project_id=self._id,
                                                 **data.as_dict())
            has_bonus = False
            if cfg.CONF.enable_bonus and data['type'] != 'bonus':
                data['type'] = 'bonus'
                data['come_from'] = 'system'
                value = gringutils.calculate_bonus(data['value'])
                if value > 0:
                    data['value'] = value
                    bonus, _ = self.conn.update_account(request.context,
                                                        None,
                                                        project_id=self._id,
                                                        **data.as_dict())
                    has_bonus = True

            self.conn.set_charged_orders(request.context, None, project_id=self._id)
        except exception.NotAuthorized as e:
            LOG.exception('Fail to charge the account:%s due to not authorization' % \
                          self._id)
            raise exception.NotAuthorized()
        except Exception as e:
            LOG.exception('Fail to charge the account:%s, charge value: %s' % \
                          (self._id, data.value))
            raise exception.DBError(reason=e)
        else:
            # Notifier account
            if cfg.CONF.notify_account_charged and charge['type'] != 'bonus':
                account = self.conn.get_account(request.context, None, project_id=self._id).as_dict()
                contact = keystone.get_uos_user(account['user_id'])
                self.notifier = notifier.NotifierService(cfg.CONF.checker.notifier_level)
                self.notifier.notify_account_charged(request.context,
                                                     account,
                                                     contact,
                                                     data['type'],
                                                     charge.value,
                                                     bonus = bonus.value if has_bonus else 0)
        return models.Charge.from_db_model(charge)

    @wsexpose(models.Charges, datetime.datetime, datetime.datetime, int, int)
    def charges(self, start_time=None, end_time=None, limit=None, offset=None):
        """Return this account's charge records
        """
        self.conn = pecan.request.db_conn
        charges = self.conn.get_charges(request.context,
                                        project_id=self._id,
                                        limit=limit,
                                        offset=offset,
                                        start_time=start_time,
                                        end_time=end_time)
        charges_list = []
        for charge in charges:
            charges_list.append(models.Charge.from_db_model(charge))

        total_price, total_count = self.conn.get_charges_price_and_count(
            request.context, project_id=self._id,
            start_time=start_time, end_time=end_time)
        total_price = gringutils._quantize_decimal(total_price)

        return models.Charges.transform(total_price=total_price,
                                        total_count=total_count,
                                        charges=charges_list)
    @wsexpose(int)
    def estimate(self):
        self.conn = pecan.request.db_conn

        if not cfg.CONF.enable_owe:
            return -1

        account = self._account()
        if account.balance < 0:
            return -2

        orders = self.conn.get_active_orders(request.context,
                                             project_id=self._id,
                                             within_one_hour=True)
        if not orders:
            return -1

        price_per_hour = 0
        for order in orders:
            price_per_hour += gringutils._quantize_decimal(order.unit_price)

        if price_per_hour == 0:
            return -1

        price_per_day = price_per_hour * 24
        days_to_owe_d = float(account.balance / price_per_day)
        days_to_owe = round(days_to_owe_d)
        if days_to_owe < days_to_owe_d:
            days_to_owe = days_to_owe + 1
        if days_to_owe > 7:
            return -1
        return days_to_owe

    @wsexpose(float)
    def estimate_per_day(self):
        self.conn = pecan.request.db_conn

        account = self._account()
        orders = self.conn.get_active_orders(request.context,
                                             project_id=self._id,
                                             within_one_hour=True)
        if not orders:
            return 0

        price_per_hour = 0
        for order in orders:
            price_per_hour += gringutils._quantize_decimal(order.unit_price)

        if price_per_hour == 0:
            return 0

        price_per_day = price_per_hour * 24
        return round(float(price_per_day), 4)


class AccountsController(rest.RestController):
    """Manages operations on the accounts collection
    """
    @pecan.expose()
    def _lookup(self, project_id, *remainder):
        if remainder and not remainder[-1]:
            remainder = remainder[:-1]
        return AccountController(project_id), remainder

    @wsexpose([models.AdminAccount], bool, int, int)
    def get_all(self, owed=None, limit=None, offset=None):
        """Get all accounts
        """
        self.conn = pecan.request.db_conn

        try:
            accounts = self.conn.get_accounts(request.context, owed=owed,
                                              limit=limit, offset=offset)
            count = self.conn.get_accounts_count(request.context, owed=owed)
            pecan.response.headers['X-Total-Count'] = str(count)
        except exception.NotAuthorized as e:
            LOG.exception('Fail to get all accounts')
            raise exception.NotAuthorized()
        except Exception as e:
            LOG.exception('Fail to get all accounts')
            raise exception.DBError(reason=e)

        return [models.AdminAccount.from_db_model(account)
                for account in accounts]


    @wsexpose(None, body=models.AdminAccount)
    def post(self, data):
        """Create a new account
        """
        conn = pecan.request.db_conn
        try:
            account = db_models.Account(**data.as_dict())
            return conn.create_account(request.context, account)
        except Exception:
            LOG.exception('Fail to create account: %s' % data.as_dict())
            raise exception.AccountCreateFailed(project_id=data.project_id)
