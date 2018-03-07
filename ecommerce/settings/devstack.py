"""Devstack settings"""
from os import environ

import yaml

from ecommerce.settings.base import *
from ecommerce.settings.logger import get_logger_config

LOGGING = get_logger_config(debug=True, dev_env=True, local_loglevel='DEBUG')

# Pull in base setting overrides from configuration file.
CONFIG_FILE = environ.get('ECOMMERCE_CFG')
if CONFIG_FILE is not None:
    with open(CONFIG_FILE) as f:
        overrides = yaml.load(f)
        vars().update(overrides)

DEBUG = True
ENABLE_AUTO_AUTH = True


# PAYMENT PROCESSING
PAYMENT_PROCESSOR_CONFIG = {
    'edx': {
        'cybersource': {
            'soap_api_url': 'https://ics2wstest.ic3.com/commerce/1.x/transactionProcessor/CyberSourceTransaction_1.115.wsdl',
            'merchant_id': 'fake-merchant-id',
            'transaction_key': 'fake-transaction-key',
            'profile_id': 'fake-profile-id',
            'access_key': 'fake-access-key',
            'secret_key': 'fake-secret-key',
            'payment_page_url': 'https://testsecureacceptance.cybersource.com/pay',
            'receipt_path': PAYMENT_PROCESSOR_RECEIPT_PATH,
            'cancel_path': PAYMENT_PROCESSOR_CANCEL_PATH,
            'send_level_2_3_details': True,
        },
        'paypal': {
            'mode': 'sandbox',
            'client_id': 'AT8uLpND3ssYmitAF60U2FfwOS9K_JEVTEI1rOm_RMYUCXvK9zesus1MQOt0IhjAKDdUNVYBmy_md2YS',
            'client_secret': 'EE4OIlmL5RybngfgeN3XEJxPq2BohiEXDYbww0ggKdU6C029yHkFlhncfYcPXGsDAUmP5dwKTI_FakfJ',
            'receipt_path': 'http://localhost:8000/commerce/checkout/receipt/',
            'cancel_path': 'http://localhost:8000/commerce/checkout/cancel/',
            'error_path': 'http://localhost:8000/commerce/checkout/error/',
        },
        'dumpay': {
            'mode': 'sandbox',
            'client_id': 'AT8uLpND3ssYmitAF60U2FfwOS9K_JEVTEI1rOm_RMYUCXvK9zesus1MQOt0IhjAKDdUNVYBmy_md2YS',
            'client_secret': 'EE4OIlmL5RybngfgeN3XEJxPq2BohiEXDYbww0ggKdU6C029yHkFlhncfYcPXGsDAUmP5dwKTI_FakfJ',
            'receipt_path': 'http://localhost:8000/commerce/checkout/receipt/',
            'cancel_path': 'http://localhost:8000/commerce/checkout/cancel/',
            'error_path': 'http://localhost:8000/commerce/checkout/error/',
        },
    },
}
# END PAYMENT PROCESSING

# Load private settings
if os.path.isfile(join(dirname(abspath(__file__)), 'private.py')):
    from .private import *  # pylint: disable=import-error
