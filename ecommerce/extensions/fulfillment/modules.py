"""Fulfillment Modules with specific fulfillment logic per Product Type, or a Combination of Types

Fulfillment Modules are designed to allow specific fulfillment logic based on the type (or types) of products
in an Order.
"""
import abc
import datetime
import json
import logging

import requests
from django.conf import settings
from django.urls import reverse
from edx_rest_api_client.client import EdxRestApiClient
from oscar.core.loading import get_model
from requests.exceptions import ConnectionError, Timeout  # pylint: disable=ungrouped-imports
from rest_framework import status

from ecommerce.core.constants import (
    DONATIONS_FROM_CHECKOUT_TESTS_PRODUCT_TYPE_NAME,
    ENROLLMENT_CODE_PRODUCT_CLASS_NAME
)
from ecommerce.core.url_utils import get_lms_enrollment_api_url, get_lms_entitlement_api_url
from ecommerce.courses.models import Course
from ecommerce.courses.utils import mode_for_product
from ecommerce.enterprise.utils import get_or_create_enterprise_customer_user
from ecommerce.extensions.analytics.utils import audit_log, parse_tracking_context
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.fulfillment.status import LINE
from ecommerce.extensions.voucher.models import OrderLineVouchers
from ecommerce.extensions.voucher.utils import create_vouchers
from ecommerce.notifications.notifications import send_notification

Benefit = get_model('offer', 'Benefit')
Option = get_model('catalogue', 'Option')
Product = get_model('catalogue', 'Product')
Range = get_model('offer', 'Range')
Voucher = get_model('voucher', 'Voucher')
logger = logging.getLogger(__name__)


class BaseFulfillmentModule(object):  # pragma: no cover
    """
    Base FulfillmentModule class for containing Product specific fulfillment logic.

    All modules should extend the FulfillmentModule and adhere to the defined contract.
    """
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def supports_line(self, line):
        """
        Returns True if the given Line can be fulfilled/revoked by this module.

        Args:
            line (Line): Line to be considered.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_supported_lines(self, lines):
        """ Return a list of supported lines

        Each Fulfillment Module is capable of fulfillment certain products. This function allows a preliminary
        check of which lines could be supported by this Fulfillment Module.

         By evaluating the lines, this will return a list of all the lines in the order that
         can be fulfilled by this module.

        Args:
            lines (List of Lines): Order Lines, associated with purchased products in an Order.

        Returns:
            A supported list of lines, unmodified.
        """
        raise NotImplementedError("Line support method not implemented!")

    @abc.abstractmethod
    def fulfill_product(self, order, lines):
        """ Fulfills the specified lines in the order.

        Iterates over the given lines and fulfills the associated products. Will report success if the product can
        be fulfilled, but may fail if the module cannot support fulfillment of the specified product, or there is
        an error with the services required to fulfill the current product.

        Args:
            order (Order): The Order associated with the lines to be fulfilled
            lines (List of Lines): Order Lines, associated with purchased products in an Order.

        Returns:
            The original set of lines, with new statuses set based on the success or failure of fulfillment.

        """
        raise NotImplementedError("Fulfillment method not implemented!")

    @abc.abstractmethod
    def revoke_line(self, line):
        """ Revokes the specified line.

        Args:
            line (Line): Order Line to be revoked.

        Returns:
            True, if the product is revoked; otherwise, False.
        """
        raise NotImplementedError("Revoke method not implemented!")


class DonationsFromCheckoutTestFulfillmentModule(BaseFulfillmentModule):
    """
    Fulfillment module for fulfilling donations as a part of LEARNER-2842 - Test Donations on Checkout.
    If that test, or any follow up tests around donations at checkout are not implemented, this module will be reverted.
    Don't use this code for your own purposes, thanks.
    """
    def supports_line(self, line):
        """
        Returns True if the given Line has a donation product.
        """
        return line.product.get_product_class().name == DONATIONS_FROM_CHECKOUT_TESTS_PRODUCT_TYPE_NAME

    def get_supported_lines(self, lines):
        """ Return a list of supported lines (that contain a donation product)

        Args:
            lines (List of Lines): Order Lines, associated with purchased products in an Order.

        Returns:
            A supported list of lines, unmodified.
        """
        return [line for line in lines if self.supports_line(line)]

    def fulfill_product(self, order, lines):
        """ Fulfills the specified lines in the order.
        Marks the line status as complete. Does not change anything else.

        Args:
            order (Order): The Order associated with the lines to be fulfilled
            lines (List of Lines): Order Lines, associated with purchased products in an Order.

        Returns:
            The original set of lines, with new statuses set based on the success or failure of fulfillment.

        """
        for line in lines:
            line.set_status(LINE.COMPLETE)
        return order, lines

    def revoke_line(self, line):
        """ Revokes the specified line.
        (Returning true to avoid unnecessary errors)

        Args:
            line (Line): Order Line to be revoked.

        Returns:
            True, if the product is revoked; otherwise, False.
        """
        return True


class EnrollmentFulfillmentModule(BaseFulfillmentModule):
    """ Fulfillment Module for enrolling students after a product purchase.

    Allows the enrollment of a student via purchase of a 'seat'.
    """

    def _post_to_enrollment_api(self, data, user):
        enrollment_api_url = get_lms_enrollment_api_url()
        timeout = settings.ENROLLMENT_FULFILLMENT_TIMEOUT
        headers = {
            'Content-Type': 'application/json',
            'X-Edx-Api-Key': settings.EDX_API_KEY
        }

        __, client_id, ip = parse_tracking_context(user)

        if client_id:
            headers['X-Edx-Ga-Client-Id'] = client_id

        if ip:
            headers['X-Forwarded-For'] = ip

        return requests.post(enrollment_api_url, data=json.dumps(data), headers=headers, timeout=timeout)

    def _add_enterprise_data_to_enrollment_api_post(self, data, order):
        """ Augment enrollment api POST data with enterprise specific data.

        Checks the order to see if there was a discount applied and if that discount
        was associated with an EnterpriseCustomer. If so, enterprise specific data
        is added to the POST data and an EnterpriseCustomerUser model is created if
        one does not already exist.

        Arguments:
            data (dict): The POST data for the enrollment API.
            order (Order): The order.
        """
        # Collect the EnterpriseCustomer UUID from the coupon, if any.
        enterprise_customer_uuid = None
        for discount in order.discounts.all():
            try:
                enterprise_customer_uuid = discount.voucher.benefit.range.enterprise_customer
            except AttributeError:
                # The voucher did not have an enterprise customer associated with it.
                pass

            if enterprise_customer_uuid is not None:
                data['linked_enterprise_customer'] = str(enterprise_customer_uuid)
                break

        # If an EnterpriseCustomer UUID is associated with the coupon, create an EnterpriseCustomerUser
        # on the Enterprise service if one doesn't already exist.
        if enterprise_customer_uuid is not None:
            get_or_create_enterprise_customer_user(
                order.site,
                enterprise_customer_uuid,
                order.user.username
            )

    def supports_line(self, line):
        return line.product.is_seat_product

    def get_supported_lines(self, lines):
        """ Return a list of lines that can be fulfilled through enrollment.

        Checks each line to determine if it is a "Seat". Seats are fulfilled by enrolling students
        in a course, which is the sole functionality of this module. Any Seat product will be returned as
        a supported line.

        Args:
            lines (List of Lines): Order Lines, associated with purchased products in an Order.

        Returns:
            A supported list of unmodified lines associated with "Seat" products.
        """
        return [line for line in lines if self.supports_line(line)]

    def fulfill_product(self, order, lines):
        """ Fulfills the purchase of a 'seat' by enrolling the associated student.

        Uses the order and the lines to determine which courses to enroll a student in, and with certain
        certificate types. May result in an error if the Enrollment API cannot be reached, or if there is
        additional business logic errors when trying to enroll the student.

        Args:
            order (Order): The Order associated with the lines to be fulfilled. The user associated with the order
                is presumed to be the student to enroll in a course.
            lines (List of Lines): Order Lines, associated with purchased products in an Order. These should only
                be "Seat" products.

        Returns:
            The original set of lines, with new statuses set based on the success or failure of fulfillment.

        """
        logger.info("Attempting to fulfill 'Seat' product types for order [%s]", order.number)

        api_key = getattr(settings, 'EDX_API_KEY', None)
        if not api_key:
            logger.error(
                'EDX_API_KEY must be set to use the EnrollmentFulfillmentModule'
            )
            for line in lines:
                line.set_status(LINE.FULFILLMENT_CONFIGURATION_ERROR)

            return order, lines

        for line in lines:
            try:
                mode = mode_for_product(line.product)
                course_key = line.product.attr.course_key
            except AttributeError:
                logger.error("Supported Seat Product does not have required attributes, [certificate_type, course_key]")
                line.set_status(LINE.FULFILLMENT_CONFIGURATION_ERROR)
                continue
            try:
                provider = line.product.attr.credit_provider
            except AttributeError:
                logger.debug("Seat [%d] has no credit_provider attribute. Defaulted to None.", line.product.id)
                provider = None

            data = {
                'user': order.user.username,
                'is_active': True,
                'mode': mode,
                'course_details': {
                    'course_id': course_key
                },
                'enrollment_attributes': [
                    {
                        'namespace': 'order',
                        'name': 'order_number',
                        'value': order.number
                    }
                ]
            }
            if provider:
                data['enrollment_attributes'].append(
                    {
                        'namespace': 'credit',
                        'name': 'provider_id',
                        'value': provider
                    }
                )
            try:
                self._add_enterprise_data_to_enrollment_api_post(data, order)

                # Post to the Enrollment API. The LMS will take care of posting a new EnterpriseCourseEnrollment to
                # the Enterprise service if the user+course has a corresponding EnterpriseCustomerUser.
                response = self._post_to_enrollment_api(data, user=order.user)

                if response.status_code == status.HTTP_200_OK:
                    line.set_status(LINE.COMPLETE)

                    audit_log(
                        'line_fulfilled',
                        order_line_id=line.id,
                        order_number=order.number,
                        product_class=line.product.get_product_class().name,
                        course_id=course_key,
                        mode=mode,
                        user_id=order.user.id,
                        credit_provider=provider,
                    )
                else:
                    try:
                        data = response.json()
                        reason = data.get('message')
                    except Exception:  # pylint: disable=broad-except
                        reason = '(No detail provided.)'

                    logger.error(
                        "Fulfillment of line [%d] on order [%s] failed with status code [%d]: %s",
                        line.id, order.number, response.status_code, reason
                    )
                    line.set_status(LINE.FULFILLMENT_SERVER_ERROR)
            except ConnectionError:
                logger.error(
                    "Unable to fulfill line [%d] of order [%s] due to a network problem", line.id, order.number
                )
                line.set_status(LINE.FULFILLMENT_NETWORK_ERROR)
            except Timeout:
                logger.error(
                    "Unable to fulfill line [%d] of order [%s] due to a request time out", line.id, order.number
                )
                line.set_status(LINE.FULFILLMENT_TIMEOUT_ERROR)
        logger.info("Finished fulfilling 'Seat' product types for order [%s]", order.number)
        return order, lines

    def revoke_line(self, line):
        try:
            logger.info('Attempting to revoke fulfillment of Line [%d]...', line.id)

            mode = mode_for_product(line.product)
            course_key = line.product.attr.course_key
            data = {
                'user': line.order.user.username,
                'is_active': False,
                'mode': mode,
                'course_details': {
                    'course_id': course_key,
                },
            }

            response = self._post_to_enrollment_api(data, user=line.order.user)

            if response.status_code == status.HTTP_200_OK:
                audit_log(
                    'line_revoked',
                    order_line_id=line.id,
                    order_number=line.order.number,
                    product_class=line.product.get_product_class().name,
                    course_id=course_key,
                    certificate_type=getattr(line.product.attr, 'certificate_type', ''),
                    user_id=line.order.user.id
                )

                return True
            else:
                # check if the error / message are something we can recover from.
                data = response.json()
                detail = data.get('message', '(No details provided.)')
                if response.status_code == 400 and "Enrollment mode mismatch" in detail:
                    # The user is currently enrolled in different mode than the one
                    # we are refunding an order for.  Don't revoke that enrollment.
                    logger.info('Skipping revocation for line [%d]: %s', line.id, detail)
                    return True
                else:
                    logger.error('Failed to revoke fulfillment of Line [%d]: %s', line.id, detail)
        except Exception:  # pylint: disable=broad-except
            logger.exception('Failed to revoke fulfillment of Line [%d].', line.id)

        return False


class CouponFulfillmentModule(BaseFulfillmentModule):
    """ Fulfillment Module for coupons. """

    def supports_line(self, line):
        """
        Check whether the product in line is a Coupon

        Args:
            line (Line): Line to be considered.

        Returns:
            True if the line contains product of product class Coupon.
            False otherwise.
        """
        return line.product.is_coupon_product

    def get_supported_lines(self, lines):
        """ Return a list of lines containing products with Coupon product class
        that can be fulfilled.

        Args:
            lines (List of Lines): Order Lines, associated with purchased products in an Order.
        Returns:
            A supported list of unmodified lines associated with 'Coupon' products.
        """
        return [line for line in lines if self.supports_line(line)]

    def fulfill_product(self, order, lines):
        """ Fulfills the purchase of an 'coupon' products.

        Args:
            order (Order): The Order associated with the lines to be fulfilled.
            lines (List of Lines): Order Lines, associated with purchased products in an Order. These should only
                be 'Coupon' products.
        Returns:
            The original set of lines, with new statuses set based on the success or failure of fulfillment.
        """
        logger.info("Attempting to fulfill 'Coupon' product types for order [%s]", order.number)

        for line in lines:
            line.set_status(LINE.COMPLETE)

        logger.info("Finished fulfilling 'Coupon' product types for order [%s]", order.number)
        return order, lines

    def revoke_line(self, line):
        """ Revokes the specified line.

        Args:
            line (Line): Order Line to be revoked.

        Returns:
            True, if the product is revoked; otherwise, False.
        """
        raise NotImplementedError("Revoke method not implemented!")


class EnrollmentCodeFulfillmentModule(BaseFulfillmentModule):
    def supports_line(self, line):
        """
        Check whether the product in line is an Enrollment code.

        Args:
            line (Line): Line to be considered.

        Returns:
            True if the line contains an Enrollment code.
            False otherwise.
        """
        return line.product.is_enrollment_code_product

    def get_supported_lines(self, lines):
        """ Return a list of lines containing Enrollment code products that can be fulfilled.

        Args:
            lines (List of Lines): Order Lines, associated with purchased products in an Order.
        Returns:
            A supported list of unmodified lines associated with an Enrollment code product.
        """
        return [line for line in lines if self.supports_line(line)]

    def fulfill_product(self, order, lines):
        """ Fulfills the purchase of an Enrollment code product.
        For each line creates number of vouchers equal to that line's quantity. Creates a new OrderLineVouchers
        object to tie the order with the created voucher and adds the vouchers to the coupon's total vouchers.

        Args:
            order (Order): The Order associated with the lines to be fulfilled.
            lines (List of Lines): Order Lines, associated with purchased products in an Order.

        Returns:
            The original set of lines, with new statuses set based on the success or failure of fulfillment.
        """
        msg = "Attempting to fulfill '{product_class}' product types for order [{order_number}]".format(
            product_class=ENROLLMENT_CODE_PRODUCT_CLASS_NAME,
            order_number=order.number
        )
        logger.info(msg)

        for line in lines:
            name = 'Enrollment Code Range for {}'.format(line.product.attr.course_key)
            seat = Product.objects.filter(
                attributes__name='course_key',
                attribute_values__value_text=line.product.attr.course_key
            ).get(
                attributes__name='certificate_type',
                attribute_values__value_text=line.product.attr.seat_type
            )
            _range, created = Range.objects.get_or_create(name=name)
            if created:
                _range.add_product(seat)

            vouchers = create_vouchers(
                name='Enrollment code voucher [{}]'.format(line.product.title),
                benefit_type=Benefit.PERCENTAGE,
                benefit_value=100,
                catalog=None,
                coupon=seat,
                end_datetime=settings.ENROLLMENT_CODE_EXIPRATION_DATE,
                enterprise_customer=None,
                quantity=line.quantity,
                start_datetime=datetime.datetime.now(),
                voucher_type=Voucher.SINGLE_USE,
                _range=_range
            )

            line_vouchers = OrderLineVouchers.objects.create(line=line)
            for voucher in vouchers:
                line_vouchers.vouchers.add(voucher)

            line.set_status(LINE.COMPLETE)

        self.send_email(order)
        logger.info("Finished fulfilling 'Enrollment code' product types for order [%s]", order.number)
        return order, lines

    def revoke_line(self, line):
        """ Revokes the specified line.

        Args:
            line (Line): Order Line to be revoked.

        Returns:
            True, if the product is revoked; otherwise, False.
        """
        raise NotImplementedError("Revoke method not implemented!")

    def send_email(self, order):
        """ Sends an email with enrollment code order information. """
        # Note (multi-courses): Change from a course_name to a list of course names.
        product = order.lines.first().product
        course = Course.objects.get(id=product.attr.course_key)
        receipt_page_url = get_receipt_page_url(
            order_number=order.number,
            site_configuration=order.site.siteconfiguration
        )
        send_notification(
            order.user,
            'ORDER_WITH_CSV',
            context={
                'contact_url': order.site.siteconfiguration.build_lms_url('/contact'),
                'course_name': course.name,
                'download_csv_link': order.site.siteconfiguration.build_ecommerce_url(
                    reverse('coupons:enrollment_code_csv', args=[order.number])
                ),
                'enrollment_code_title': product.title,
                'lms_url': order.site.siteconfiguration.build_lms_url(),
                'order_number': order.number,
                'partner_name': order.site.siteconfiguration.partner.name,
                'receipt_page_url': receipt_page_url,
            },
            site=order.site
        )


class CourseEntitlementFulfillmentModule(BaseFulfillmentModule):
    """ Fulfillment Module for granting students an entitlement.
    Allows the entitlement of a student via purchase of a 'Course Entitlement'.
    """

    def supports_line(self, line):
        return line.product.is_course_entitlement_product

    def get_supported_lines(self, lines):
        """ Return a list of lines that can be fulfilled.
        Checks each line to determine if it is a "Course Entitlement". Entitlements are fulfilled by granting students
        an entitlement in a course, which is the sole functionality of this module.
        Args:
            lines (List of Lines): Order Lines, associated with purchased products in an Order.
        Returns:
            A supported list of unmodified lines associated with "Course Entitlement" products.
        """
        return [line for line in lines if self.supports_line(line)]

    def fulfill_product(self, order, lines):
        """ Fulfills the purchase of a 'Course Entitlement'.
        Uses the order and the lines to determine which courses to grant an entitlement for, and with certain
        certificate types. May result in an error if the Entitlement API cannot be reached, or if there is
        additional business logic errors when trying grant the entitlement.
        Args:
            order (Order): The Order associated with the lines to be fulfilled. The user associated with the order
                is presumed to be the student to grant an entitlement.
            lines (List of Lines): Order Lines, associated with purchased products in an Order. These should only
                be "Course Entitlement" products.
        Returns:
            The original set of lines, with new statuses set based on the success or failure of fulfillment.
        """
        logger.info('Attempting to fulfill "Course Entitlement" product types for order [%s]', order.number)

        for line in lines:
            try:
                mode = mode_for_product(line.product)
                UUID = line.product.attr.UUID
            except AttributeError:
                logger.error('Entitlement Product does not have required attributes, [certificate_type, UUID]')
                line.set_status(LINE.FULFILLMENT_CONFIGURATION_ERROR)
                continue

            data = {
                'user': order.user.username,
                'course_uuid': UUID,
                'mode': mode,
                'order_number': order.number,
            }

            try:
                entitlement_option = Option.objects.get(code='course_entitlement')

                entitlement_api_client = EdxRestApiClient(
                    get_lms_entitlement_api_url(),
                    jwt=order.site.siteconfiguration.access_token
                )

                # POST to the Entitlement API.
                response = entitlement_api_client.entitlements.post(data)
                line.attributes.create(option=entitlement_option, value=response['uuid'])
                line.set_status(LINE.COMPLETE)

                audit_log(
                    'line_fulfilled',
                    order_line_id=line.id,
                    order_number=order.number,
                    product_class=line.product.get_product_class().name,
                    UUID=UUID,
                    mode=mode,
                    user_id=order.user.id,
                )
            except (Timeout, ConnectionError):
                logger.exception(
                    'Unable to fulfill line [%d] of order [%s] due to a network problem', line.id, order.number
                )
                line.set_status(LINE.FULFILLMENT_NETWORK_ERROR)
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    'Unable to fulfill line [%d] of order [%s]', line.id, order.number
                )
                line.set_status(LINE.FULFILLMENT_SERVER_ERROR)

        logger.info('Finished fulfilling "Course Entitlement" product types for order [%s]', order.number)
        return order, lines

    def revoke_line(self, line):
        try:
            logger.info('Attempting to revoke fulfillment of Line [%d]...', line.id)

            UUID = line.product.attr.UUID
            entitlement_option = Option.objects.get(code='course_entitlement')
            course_entitlement_uuid = line.attributes.get(option=entitlement_option).value

            entitlement_api_client = EdxRestApiClient(
                get_lms_entitlement_api_url(),
                jwt=line.order.site.siteconfiguration.access_token
            )

            # DELETE to the Entitlement API.
            entitlement_api_client.entitlements(course_entitlement_uuid).delete()

            audit_log(
                'line_revoked',
                order_line_id=line.id,
                order_number=line.order.number,
                product_class=line.product.get_product_class().name,
                UUID=UUID,
                certificate_type=getattr(line.product.attr, 'certificate_type', ''),
                user_id=line.order.user.id
            )

            return True
        except Exception:  # pylint: disable=broad-except
            logger.exception('Failed to revoke fulfillment of Line [%d].', line.id)

        return False

class DigitalBookFulfillmentModule(BaseFulfillmentModule):
    """ Fulfillment Module for granting students an entitlement.
    Allows the entitlement of a student via purchase of a 'Course Entitlement'.
    """

    def supports_line(self, line):
        logger.debug('Line order: [%s], is digital book: [%s]', line, line.product.is_digital_book_product)
        return line.product.is_digital_book_product

    def get_supported_lines(self, lines):
        """ Return a list of lines that can be fulfilled.
        Checks each line to determine if it is a "Digital Book". Ditigal Books are fulfilled by granting students
        an access to a digital book, which is the sole functionality of this module.
        Args:
            lines (List of Lines): Order Lines, associated with purchased products in an Order.
        Returns:
            A supported list of unmodified lines associated with "Digital Book" products.
        """
        return [line for line in lines if self.supports_line(line)]

    def fulfill_product(self, order, lines):
        """ Fulfills the purchase of a 'Digital Book'
        Args:
            order (Order): The Order associated with the lines to be fulfilled. The user associated with the order
                fis presumed to be the student to grant an entitlement.
            lines (List of Lines): Order Lines, associated with purchased products in an Order. These should only
                be "Digital Book" products.
        Returns:
            The original set of lines, with new statuses set based on the success or failure of fulfillment.
        """
        logger.info('Attempting to fulfill "Digital Book" product types for order [%s]', order.number)

        logger.info('>>> fulfill_product...')

        for line in lines:
            logger.info('>>> line: %s', line)
            try:
                book_key = line.product.attr.book_key
                logger.info('>>> Book Key: %s', book_key)
            except AttributeError:
                logger.error('Digital Book Product does not have required attributes, [book_key]')
                line.set_status(LINE.FULFILLMENT_CONFIGURATION_ERROR)
                continue

            data = {
                'user': order.user.username,
                'book_key': book_key,
                'order_number': order.number,
            }

            logger.info('>>> data: user: [%s], book_key: [%s], order_number: [%s]', data['user'], data['book_key'], data['order_number'] )

        return False

    def revoke_line(self, line):
        logger.error('REVOKE LINE')
        return False
