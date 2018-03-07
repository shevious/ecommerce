from django.http import HttpResponse
from django.views.generic import View
from django.template import loader
from django.shortcuts import render
from ecommerce.extensions.payment.dumpayform import DumpayForm
from django.http import HttpResponseRedirect

from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
import logging
from oscar.core.loading import get_class, get_model
from oscar.apps.payment.exceptions import PaymentError, UserCancelled, TransactionDeclined

from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.payment.processors.dumpay import Dumpay
from ecommerce.core.url_utils import get_ecommerce_url
from django.core.urlresolvers import reverse

from django.shortcuts import redirect
from urlparse import urljoin

Basket = get_model('basket', 'Basket')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')

logger = logging.getLogger(__name__)

class DumpayPaymentCallView(View):

    @property
    def payment_processor(self):
        return Dumpay()

    template_name = 'dumpay/post.html'
    # Disable atomicity for the view. Otherwise, we'd be unable to commit to the database
    # until the request had concluded; Django will refuse to commit when an atomic() block
    # is active, since that would break atomicity. Without an order present in the database
    # at the time fulfillment is attempted, asynchronous order fulfillment tasks will fail.
    @method_decorator(transaction.non_atomic_requests)
    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super(DumpayPaymentCallView, self).dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = DumpayForm()
        template = loader.get_template(self.template_name)
        context = {
            'form': form
        }
        return HttpResponse(template.render(context, request))

    def post(self, request, *args, **kwargs):
        form = DumpayForm(request.POST)
        if not form.is_valid():
            # <process form cleaned data>
            context = {
                'form': form
            }
            template = loader.get_template(self.template_name)
            return HttpResponse(template.render(context, request))

        logger.info(form)
        #logger.info( request)
        #logger.info( request.POST.get('basket_id'))
        logger.info(request.GET.get('basket_id'))
        basket_id = request.GET.get('basket_id')
        basket = Basket.objects.get(id=basket_id)
        basket.strategy = request.strategy
        logger.info(basket)
        dumpay_response = {
            "transaction_id": form.cleaned_data['transaction_id'],
            "payer_id": form.cleaned_data['payer_id'],
            "basekt_id": basket_id
        }
        receipt_url = u'{}?orderNum={}'.format(self.payment_processor.receipt_url, basket.order_number)
        url = urljoin(get_ecommerce_url(), reverse('dumpay_execute'))
        #execute_url = u'{}?transactionId={}&payerId={}&basketId={}'.format(url,form.cleaned_data['transaction_id'],form.cleaned_data['payer_id'], basket_id)
        execute_url = u'{}?transactionId={}&payerId={}&basketId={}'.format(url,basket.order_number,form.cleaned_data['payer_id'], basket_id)
        logger.info(dumpay_response)
        return redirect(execute_url)
        try:
            with transaction.atomic():
                try:
                    self.handle_payment(dumpay_response, basket)
                except PaymentError:
                    return redirect(self.payment_processor.error_url)
        except:  # pylint: disable=bare-except
            logger.exception('Attempts to handle payment for basket [%d] failed.', basket.id)
            return redirect(receipt_url)
        #post_data = [('name','Gladys'),]
        #return HttpResponseRedirect('/success/')
        try:
            # Note (CCB): In the future, if we do end up shipping physical products, we will need to
            # properly implement shipping methods. For more, see
            # http://django-oscar.readthedocs.org/en/latest/howto/how_to_configure_shipping.html.
            shipping_method = NoShippingRequired()
            shipping_charge = shipping_method.calculate(basket)

            # Note (CCB): This calculation assumes the payment processor has not sent a partial authorization,
            # thus we use the amounts stored in the database rather than those received from the payment processor.
            order_total = OrderTotalCalculator().calculate(basket, shipping_charge)

            user = basket.owner
            # Given a basket, order number generation is idempotent. Although we've already
            # generated this order number once before, it's faster to generate it again
            # than to retrieve an invoice number from PayPal.
            order_number = basket.order_number

            self.handle_order_placement(
                order_number,
                user,
                basket,
                None,
                shipping_method,
                shipping_charge,
                None,
                order_total
            )

            return redirect(receipt_url)
        except:  # pylint: disable=bare-except
            logger.exception(self.order_placement_failure_msg, basket.id)
            return redirect(receipt_url)


'''
    def get(self, request, *args, **kwargs):
        #getting our template
        template = loader.get_template('dumpay/index.html')
     
        #creating the values to pass
        context = {
            'name':'Belal Khan',
            'fname':'Azad Khan',
            'course':'Python Django Framework',
            'address':'Kanke, Ranchi, India',
        }
     
        #rendering the template in HttpResponse
        #but this time passing the context and request
        return HttpResponse(template.render(context, request))
'''
'''
        num_books = 10
        num_instances = 20
        num_instances_available = 30
        num_authors = 40
        return render(
                request,
                'dumpay/dumpay.html',
                context={'num_books':num_books,'num_instances':num_instances,'num_instances_available':num_instances_available,'num_authors':num_authors},
                )
'''
        #return HttpResponse("Hello, World")
