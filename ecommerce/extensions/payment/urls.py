""" Payment-related URLs """
from django.conf.urls import url

from ecommerce.extensions.payment import views
from ecommerce.extensions.payment import dumpayviews

urlpatterns = [
    url(r'^cybersource/notify/$', views.CybersourceNotifyView.as_view(), name='cybersource_notify'),
    url(r'^paypal/execute/$', views.PaypalPaymentExecutionView.as_view(), name='paypal_execute'),
    url(r'^paypal/profiles/$', views.PaypalProfileAdminView.as_view(), name='paypal_profiles'),
    url(r'^dumpay/execute/$', views.DumpayPaymentExecutionView.as_view(), name='dumpay_execute'),
    url(r'^dumpay/call/$', dumpayviews.DumpayPaymentCallView.as_view(), name='dumpay_call'),
]
