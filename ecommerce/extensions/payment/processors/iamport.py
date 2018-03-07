# -*- coding: utf-8 -*-
import sys

sys.path.append("/edx/app/ecommerce/venvs/ecommerce/lib/python2.7/site-packages/iamport")
from client import Iamport as iamport_pay
import logging
from ecommerce.extensions.payment.processors import BasePaymentProcessor
from ecommerce.extensions.order.constants import PaymentEventTypeName
from oscar.core.loading import get_class, get_model
from django.utils.functional import cached_property
from ecommerce.core.url_utils import get_lms_url
from decimal import Decimal
from oscar.apps.payment.exceptions import GatewayError

logger = logging.getLogger(__name__)


Basket = get_model('basket', 'Basket')
PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')
ProductClass = get_model('catalogue', 'ProductClass')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')


class Iamport(BasePaymentProcessor):
    NAME = u'iamport'

    def __init__(self):
        print 'Iamport __init__ ------------- s'
        pass

    @cached_property
    def iamport_api(self):
        return iamport_pay(imp_key=self.configuration['imp_key'], imp_secret=self.configuration['imp_secret'])

    @property
    def receipt_url(self):
        return get_lms_url(self.configuration['receipt_path'])

    @property
    def cancel_url(self):
        return get_lms_url(self.configuration['cancel_path'])

    @property
    def error_url(self):
        return get_lms_url(self.configuration['error_path'])

    def get_transaction_parameters(self, basket, request=None):
        processor_response = {'order_number': basket.order_number, 'amount': basket.total_excl_tax, 'basket_id': basket.id, 'cancel_url': self.cancel_url, 'currency': basket.currency}
        transaction_id = '{0}_{1}'.format(basket.order_number, basket.id)
        self.record_processor_response(processor_response, transaction_id=transaction_id, basket=basket)
        parameters = {'payment_page_url': u'/payment/iamport/', 'order_number': basket.order_number, 'amount': basket.total_excl_tax, 'basket_id': basket.id, 'cancel_url': self.cancel_url, 'currency': basket.currency}
        return parameters

    def handle_processor_response(self, response, basket=None):

        iamport = self.iamport_api

        if response['method'] == 'insert':
            merchant_uid = response['merchant_uid']
            price = response['amount']
            pay_data = iamport.find(merchant_uid=merchant_uid)
            price = int(price)
            self.record_processor_response({'message': 'Payment Submitted', 'response': pay_data}, transaction_id=merchant_uid, basket=basket)

            # 결제 완료
            if iamport.is_paid(price, merchant_uid=merchant_uid) == True:
                self.record_processor_response({'message': 'Payment Complete', 'response': pay_data}, transaction_id=merchant_uid, basket=basket)
                source_type, __ = SourceType.objects.get_or_create(name=self.NAME)

                currency = pay_data['currency']
                total = Decimal(pay_data['amount'])
                transaction_id = pay_data['imp_uid']
                label = pay_data['buyer_email'] + '_' + pay_data['pay_method']
                card_type = pay_data['card_name']

                source = Source(
                            source_type=source_type,
                            currency=currency,
                            amount_allocated=total,
                            amount_debited=total,
                            reference=transaction_id,
                            label=label,
                            card_type=card_type
                        )

                event_type, __ = PaymentEventType.objects.get_or_create(name=PaymentEventTypeName.PAID)
                event = PaymentEvent(event_type=event_type, amount=total, reference=transaction_id, processor_name=self.NAME)

                return source, event

            else:
                try:
                    cancel_data = iamport.cancel(u'오류로인한 취소', merchant_uid=merchant_uid)
                    if cancel_data['status'] == u'cancelled':
                        logger.error(u'오류로인해 취소되었습니다. - basket_id : [%s]', basket.id)
                        self.record_processor_response({'message': 'error', 'response': cancel_data}, transaction_id=merchant_uid, basket=basket)
                        raise GatewayError
                except iamport_pay.ResponseError as e:
                    print e.code
                    print e.message  # 에러난 이유를 알 수 있음
                    logger.error(e.code)
                    logger.error(e.message)
                except iamport_pay.HttpError as http_error:
                    print http_error.code
                    print http_error.reason  # HTTP not 200 에러난 이유를 알 수 있음

    def issue_credit(self, source, amount, currency):
        order = source.order

        try:
            iamport = self.iamport_api
            basket = order.basket
            refund_data = iamport.cancel(u'환불 승인', imp_uid=source.reference, amount=amount)

            if refund_data['status'] == u'cancelled':
                transaction_id = refund_data['merchant_uid']
                self.record_processor_response({'message': 'refund success', 'response': refund_data}, transaction_id=transaction_id, basket=basket)

                source.refund(amount, reference=transaction_id)

                event_type, __ = PaymentEventType.objects.get_or_create(name=PaymentEventTypeName.REFUNDED)
                PaymentEvent.objects.create(event_type=event_type, order=order, amount=amount, reference=transaction_id,
                                            processor_name=self.NAME)
        except Exception:
            transaction_id = '{0}_{1}'.format(order.number, order.basket_id)
            entry = self.record_processor_response({'message': 'refund failed'}, transaction_id=transaction_id, basket=basket)
            logger.error(u'환불 실패 - order_number : [%s]', order.number)
            raise GatewayError

