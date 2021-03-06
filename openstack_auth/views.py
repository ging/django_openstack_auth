# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import time
import uuid
import json
import six

import django
from django.conf import settings
from django.contrib import auth
from django.contrib.auth.decorators import login_required  # noqa
from django.contrib.auth import views as django_auth_views
from django.core.cache import cache
from django import shortcuts
from django.utils import functional
from django.utils import http
from django.http.request import QueryDict
from django.views.decorators.cache import never_cache  # noqa
from django.views.decorators.csrf import csrf_protect  # noqa
from django.views.decorators.debug import sensitive_post_parameters  # noqa
from django.core.urlresolvers import reverse
from keystoneclient.auth import token_endpoint
from keystoneclient import exceptions as keystone_exceptions

from openstack_auth import forms
# This is historic and is added back in to not break older versions of
# Horizon, fix to Horizon to remove this requirement was committed in
# Juno
from openstack_auth.forms import Login  # noqa
from openstack_auth import user as auth_user
from openstack_auth import utils
from openstack_auth import exceptions

try:
    is_safe_url = http.is_safe_url
except AttributeError:
    is_safe_url = utils.is_safe_url


LOG = logging.getLogger(__name__)

LOGIN_ERROR_CODES = {
    '1': u'Invalid user name, password or verification code.',
    '2': u'Authentication time expired, please authenticate again.',
    '3': u'Something went wrong when verifying your device, please provide a code again.'
}

@sensitive_post_parameters()
@csrf_protect
@never_cache
def two_factor_login(request, template_name=None, extra_context=None, 
                             form_class=forms.TwoFactorCodeForm, **kwargs):
    """Logs a user using two factor auth
    """
    if not request.is_ajax():
        # If the user is already authenticated, redirect them to the
        # dashboard straight away, unless the 'next' parameter is set as it
        # usually indicates requesting access to a page that requires different
        # permissions.
        if (request.user.is_authenticated() and
                auth.REDIRECT_FIELD_NAME not in request.GET and
                auth.REDIRECT_FIELD_NAME not in request.POST):
            return shortcuts.redirect(settings.LOGIN_REDIRECT_URL)

    initial = {}
    if request.method == "POST":
        # NOTE(saschpe): Since https://code.djangoproject.com/ticket/15198,
        # the 'request' object is passed directly to AuthenticationForm in
        # django.contrib.auth.views#login:
        if django.VERSION >= (1, 6):
            form = functional.curry(form_class)
        else:
            form = functional.curry(form_class, request)
    else:
        form = functional.curry(form_class, initial=initial)
        application = utils.get_application(request)
        if application:
            extra_context = {}
            extra_context['next'] = request.GET.get('next')
            extra_context['show_application_details'] = True
            extra_context['application'] = application
            extra_context['redirect_field_name'] = auth.REDIRECT_FIELD_NAME

    if extra_context is None:
        extra_context = {'redirect_field_name': auth.REDIRECT_FIELD_NAME}

    if not template_name:
        if request.is_ajax():
            template_name = 'auth/_two_factor_login.html'
            extra_context['hide'] = True
        else:
            template_name = 'auth/two_factor_login.html'

    if not request.GET.get('k') or not cache.get(request.GET.get('k'), None):
        return shortcuts.redirect(settings.LOGIN_URL+'?error_code=2')

    username = cache.get(request.GET.get('k'))[0]
    domain = cache.get(request.GET.get('k'))[2]

    try:
        res = django_auth_views.login(request,
                            template_name=template_name,
                            authentication_form=form,
                            extra_context=extra_context,
                            **kwargs)
    except exceptions.KeystoneAuthException as exc:
        return shortcuts.redirect(settings.LOGIN_URL + '?error_code=1&user='+username)

    if 'remember_device' in request.POST:
        new_device_data = utils.remember_two_factor_device(username=username, domain=domain)
        cookie_data = json.dumps({'device_id': new_device_data.device_id,
                                  'device_token': new_device_data.device_token})
        res.set_signed_cookie('two-factor-auth', cookie_data)
    elif request.method == 'POST':
        res.delete_cookie('two-factor-auth')

    error_code = request.GET.get('error_code')
    if error_code:
        res.context_data['form'].errors[u'__all__'] = LOGIN_ERROR_CODES[error_code]

    # NOTE(garcianavalon) we only allow one region to log in
    # just remove the cookie to avoid issues
    # Save the region in the cookie, this is used as the default
    # selected region next time the Login form loads.
    # if request.method == "POST":
    #     utils.set_response_cookie(res, 'login_region',
    #                               request.POST.get('region', ''))

    # Set the session data here because django's session key rotation
    # will erase it if we set it earlier.
    if request.user.is_authenticated():
        auth_user.set_session_from_user(request, request.user)
        regions = dict(form_class.get_region_choices())
        region = request.user.endpoint
        region_name = regions.get(region)
        request.session['region_endpoint'] = region
        request.session['region_name'] = region_name
        request.session['last_activity'] = int(time.time())
    return res

@sensitive_post_parameters()
@csrf_protect
@never_cache
def login(request, template_name=None, extra_context=None, 
          form_class=forms.Login, **kwargs):
    """Logs a user in using the :class:`~openstack_auth.forms.Login` form."""
    if not request.is_ajax():
        # If the user is already authenticated, redirect them to the
        # dashboard straight away, unless the 'next' parameter is set as it
        # usually indicates requesting access to a page that requires different
        # permissions.
        if (request.user.is_authenticated() and
                auth.REDIRECT_FIELD_NAME not in request.GET and
                auth.REDIRECT_FIELD_NAME not in request.POST):
            return shortcuts.redirect(settings.LOGIN_REDIRECT_URL)

    # Get our initial region for the form.
    initial = {}
    current_region = request.session.get('region_endpoint', None)
    requested_region = request.GET.get('region')
    regions = dict(getattr(settings, "AVAILABLE_REGIONS", []))
    if requested_region in regions and requested_region != current_region:
        initial.update({'region': requested_region})

    if request.method == "POST":
        # NOTE(saschpe): Since https://code.djangoproject.com/ticket/15198,
        # the 'request' object is passed directly to AuthenticationForm in
        # django.contrib.auth.views#login:
        if django.VERSION >= (1, 6):
            form = functional.curry(form_class)
        else:
            form = functional.curry(form_class, request)

        # NOTE(garcianavalon) two factor support. If the user has two factor
        # enabled, cache the (username, password) and redirect to two_factor_login
        # with the Key to retrieve them      
        username = request.POST.get('username')
        password = request.POST.get('password')
        default_domain = getattr(settings,
                                 'OPENSTACK_KEYSTONE_DEFAULT_DOMAIN',
                                 'Default')
        domain = request.POST.get('domain', default_domain)

        device_data = request.get_signed_cookie('two-factor-auth', None)

        if device_data:
            try:
                device_data = json.loads(device_data)
                utils.check_for_two_factor_device(username=username,
                                                  domain=domain,
                                                  device_id=device_data['device_id'],
                                                  device_token=device_data['device_token'])
                is_two_factor_device_valid = True
            except (keystone_exceptions.Forbidden, keystone_exceptions.NotFound) as e:
                is_two_factor_device_valid = False
                if isinstance(e, keystone_exceptions.Forbidden):
                    error_code = 3

        if utils.user_has_two_factor_enabled(username=username, domain=domain) and \
           (not device_data or not is_two_factor_device_valid):
                cache_key = uuid.uuid4().hex
                cache.set(cache_key, (username, password, domain), 120)
                
                if 'next' in request.POST:
                    redirect_url = reverse('two_factor_login') + \
                        '?k={k}'.format(k=cache_key) + \
                        '&client_id={client_id}'.format(client_id=QueryDict(request.POST['next']).get('client_id')) + \
                        '&next=' + http.urlquote_plus(request.POST['next'])
                    response = shortcuts.redirect(redirect_url)
                else:
                    response = shortcuts.redirect('two_factor_login')
                    response['Location'] += '?k={k}'.format(k=cache_key)

                if 'error_code' in locals():
                    response['Location'] += '&error_code={e}'.format(e=error_code)

                return response

    else:
        form = functional.curry(form_class, initial=initial)

    if extra_context is None:
        extra_context = {'redirect_field_name': auth.REDIRECT_FIELD_NAME}

    if not template_name:
        if request.is_ajax():
            template_name = 'auth/_login.html'
            extra_context['hide'] = True
        else:
            template_name = 'auth/login.html'

    res = django_auth_views.login(request,
                                  template_name=template_name,
                                  authentication_form=form,
                                  extra_context=extra_context,
                                  **kwargs)

    if 'is_two_factor_device_valid' in locals() and is_two_factor_device_valid:
        new_device_data = utils.remember_two_factor_device(username=username,
                                                           domain=domain,
                                                           device_id=device_data['device_id'],
                                                           device_token=device_data['device_token'])
        cookie_data = json.dumps({'device_id': new_device_data.device_id,
                                  'device_token': new_device_data.device_token})
        res.set_signed_cookie('two-factor-auth', cookie_data)

    error_code = request.GET.get('error_code')
    if error_code:
        res.context_data['form'].errors[u'__all__'] = LOGIN_ERROR_CODES[error_code]
        res.context_data['form'].fields['username'].initial = request.GET.get('user')

    # NOTE(garcianavalon) we only allow one region to log in
    # just remove the cookie to avoid issues
    # Save the region in the cookie, this is used as the default
    # selected region next time the Login form loads.
    # if request.method == "POST":
    #     utils.set_response_cookie(res, 'login_region',
    #                               request.POST.get('region', ''))

    # Set the session data here because django's session key rotation
    # will erase it if we set it earlier.
    if request.user.is_authenticated():
        auth_user.set_session_from_user(request, request.user)
        regions = dict(form_class.get_region_choices())
        region = request.user.endpoint
        region_name = regions.get(region)
        request.session['region_endpoint'] = region
        request.session['region_name'] = region_name
        request.session['last_activity'] = int(time.time())
    return res


def logout(request, login_url=None, **kwargs):
    """Logs out the user if he is logged in. Then redirects to the log-in page.

    .. param:: login_url

       Once logged out, defines the URL where to redirect after login

    .. param:: kwargs
       see django.contrib.auth.views.logout_then_login extra parameters.

    """
    msg = 'Logging out user "%(username)s".' % \
        {'username': request.user.username}
    LOG.info(msg)
    endpoint = request.session.get('region_endpoint')
    token = request.session.get('token')
    if token and endpoint:
        delete_token(endpoint=endpoint, token_id=token.id)
    """ Securely logs a user out. """
    response = django_auth_views.logout_then_login(request, login_url=login_url,
                                                   **kwargs)
    # NOTE(garcianavalon) experimental!
    response.delete_cookie(settings.SESSION_COOKIE_NAME)
    return response

def delete_token(endpoint, token_id):
    """Delete a token."""
    utils.remove_project_cache(token_id)

    try:
        endpoint = utils.fix_auth_url_version(endpoint)

        session = utils.get_session()
        auth_plugin = token_endpoint.Token(endpoint=endpoint,
                                           token=token_id)
        client = utils.get_keystone_client().Client(session=session,
                                                    auth=auth_plugin)
        if utils.get_keystone_version() >= 3:
            client.tokens.revoke_token(token=token_id)
        else:
            client.tokens.delete(token=token_id)

        LOG.info('Deleted token %s' % token_id)
    except keystone_exceptions.ClientException:
        LOG.info('Could not delete token')


@login_required
def switch(request, tenant_id, redirect_field_name=auth.REDIRECT_FIELD_NAME):
    """Switches an authenticated user from one project to another."""
    LOG.debug('Switching to tenant %s for user "%s".'
              % (tenant_id, request.user.username))

    endpoint = utils.fix_auth_url_version(request.user.endpoint)
    session = utils.get_session()
    auth = utils.get_token_auth_plugin(auth_url=endpoint,
                                       token=request.user.token.id,
                                       project_id=tenant_id)

    try:
        auth_ref = auth.get_access(session)
        msg = 'Project switch successful for user "%(username)s".' % \
            {'username': request.user.username}
        LOG.info(msg)
    except keystone_exceptions.ClientException:
        msg = 'Project switch failed for user "%(username)s".' % \
            {'username': request.user.username}
        LOG.warning(msg)
        auth_ref = None
        LOG.exception('An error occurred while switching sessions.')

    # Ensure the user-originating redirection url is safe.
    # Taken from django.contrib.auth.views.login()
    redirect_to = request.REQUEST.get(redirect_field_name, '')
    if not is_safe_url(url=redirect_to, host=request.get_host()):
        redirect_to = settings.LOGIN_REDIRECT_URL

    if auth_ref:
        old_endpoint = request.session.get('region_endpoint')
        old_token = request.session.get('token')
        if old_token and old_endpoint and old_token.id != auth_ref.auth_token:
            delete_token(endpoint=old_endpoint, token_id=old_token.id)
        user = auth_user.create_user_from_token(
            request, auth_user.Token(auth_ref), endpoint)
        auth_user.set_session_from_user(request, user)
    response = shortcuts.redirect(redirect_to)
    # NOTE(garcianavalon) this recent_project cookie gives a lot or problems
    # when switching users so we don't use it anymore. Use the unscoped_auth_ref
    # utils.set_response_cookie(response, 'recent_project',
    #                           request.user.project_id)
    return response


@login_required
def switch_region(request, region_name,
                  redirect_field_name=auth.REDIRECT_FIELD_NAME):
    """Switches the user's region for all services except Identity service.

    The region will be switched if the given region is one of the regions
    available for the scoped project. Otherwise the region is not switched.
    """
    if region_name in request.user.available_services_regions:
        request.session['services_region'] = region_name
        LOG.debug('Switching services region to %s for user "%s".'
                  % (region_name, request.user.username))

    redirect_to = request.REQUEST.get(redirect_field_name, '')
    if not is_safe_url(url=redirect_to, host=request.get_host()):
        redirect_to = settings.LOGIN_REDIRECT_URL

    response = shortcuts.redirect(redirect_to)
    utils.set_response_cookie(response, 'services_region',
                              request.session['services_region'])
    return response
