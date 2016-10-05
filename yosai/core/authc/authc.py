"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
"""
from collections import defaultdict
import logging

from yosai.core import (
    AdditionalAuthenticationRequired,
    AuthenticationException,
    AuthenticationEventException,
    AuthenticationSettings,
    InvalidAuthenticationSequenceException,
    InvalidTokenException,
    LockedAccountException,
    UnknownAccountException,
    UnsupportedTokenException,
    event_abcs,
    authc_abcs,
    serialize_abcs,
    FirstRealmSuccessfulStrategy,
    DefaultAuthenticationAttempt,
    realm_abcs,
)

logger = logging.getLogger(__name__)


class UsernamePasswordToken(authc_abcs.HostAuthenticationToken,
                            authc_abcs.RememberMeAuthenticationToken):

    TIER = 1  # tier levels determine priority during multi-factor authentication

    def __init__(self, username, password, remember_me=False,
                 host=None):
        """
        :param username: the username submitted for authentication
        :type username: str

        :param password: the password submitted for authentication
        :type password: bytearray or string

        :param remember_me:  if the user wishes their identity to be
                             remembered across sessions
        :type remember_me: bool
        :param host:     the host name or IP string from where the attempt
                         is occuring
        :type host: str
        """
        self.host = host
        self.password = password
        self.is_remember_me = remember_me
        self.username = username
        self.identifier = username  # used in public api  DG:  TBD - I Dont like
        self.credentials = password  # used in public apiDG:  TBD - I Dont like

    # DG:  these properties are required implementations of the abcs

    @property
    def host(self):
        return self._host

    @host.setter
    def host(self, host):
        self._host = host

    @property
    def password(self):
        return self._password

    @password.setter
    def password(self, password):
        if not password:
            raise InvalidTokenException('Password must be defined.')

        if isinstance(password, bytearray):
            self._password = password
        if isinstance(password, str):
            self._password = bytearray(password, 'utf-8')
        else:
            raise InvalidTokenException

    @property
    def is_remember_me(self):
        return self._is_remember_me

    @is_remember_me.setter
    def is_remember_me(self, isrememberme):
        self._is_remember_me = isrememberme

    @property
    def username(self):
        return self._username

    @username.setter
    def username(self, username):
        if not username:
            raise InvalidTokenException('Username must be defined.')

        self._username = username

    @property
    def identifier(self):
        return self._identifier

    @identifier.setter
    def identifier(self, identifier):
        self._identifier = identifier

    @property
    def credentials(self):
        return self._credentials

    @credentials.setter
    def credentials(self, credentials):
        self._credentials = credentials

    def clear(self):
        self.identifier = None
        self.host = None
        self.remember_me = False

        try:
            if (self._password):
                for index in range(len(self._password)):
                    self._password[index] = 0  # DG:  this equals 0x00
        except TypeError:
            msg = 'expected password to be a bytearray'
            raise InvalidTokenException(msg)

    def __repr__(self):
        result = "{0} - {1}, remember_me={2}".format(
            self.__class__.__name__, self.username, self.is_remember_me)
        if (self.host):
            result += ", ({0})".format(self.host)
        return result

# Yosai deprecates FailedAuthenticationEvent
# Yosai deprecates SuccessfulAuthenticationEvent


class DefaultAuthenticator(authc_abcs.Authenticator, event_abcs.EventBusAware):

    # Unlike Shiro, Yosai injects the strategy and the eventbus
    def __init__(self,
                 settings,
                 strategy=FirstRealmSuccessfulStrategy(),
                 mfa_challenger=None):
        """ Default in Shiro 2.0 is 'first successful'. This is the desired
        behavior for most Shiro users (80/20 rule).  Before v2.0, was
        'at least one successful', which was often not desired and caused
        unnecessary I/O.
        """
        self.authc_settings = AuthenticationSettings(settings)
        self.authentication_strategy = strategy
        self.token_realm_resolver = self.init_token_resolution()
        self.mfa_challenger = mfa_challenger

        self.realms = None
        self.locking_realm = None
        self.locking_limit = None
        self._event_bus = None

    @property
    def event_bus(self):
        return self._event_bus

    @event_bus.setter
    def event_bus(self, eventbus):
        self._event_bus = eventbus

    def init_realms(self, realms):
        """
        :type realms: Tuple
        """
        self.realms = tuple(realm for realm in realms
                             if isinstance(realm, realm_abcs.AuthenticatingRealm))
        self.register_cache_clear_listener()
        self.init_locking()

    def init_locking(self):
        locking_limit = self.authc_settings.account_lock_threshold
        if locking_limit:
            self.locking_realm = self.locate_locking_realm()  # for account locking
            self.locking_limit = locking_limit

    def init_token_resolution(self):
        token_resolver = defaultdict(list)
        for realm in self.realms:
            if isinstance(realm, realm_abcs.AuthenticatingRealm):
                for token_class in realm.supported_tokens():
                    token_resolver[token_class].append(realm)
        return token_resolver

    def locate_locking_realm(self):
        """
        the first realm that is identified as a LockingRealm will be used to
        lock all accounts
        """
        for realm in self.realms:
            if isinstance(realm, realm_abcs.LockingRealm):
                return realm
        return None

    def authenticate_single_realm_account(self, realm, authc_token):
        return realm.authenticate_account(authc_token)

    def authenticate_multi_realm_account(self, realms, authc_token):
        attempt = DefaultAuthenticationAttempt(authc_token, realms)
        return self.authentication_strategy.execute(attempt)

    def authenticate_account(self, identifiers, authc_token):
        """
        :returns: an authenticated account
        """
        msg = ("Authentication submission received for authentication "
               "token [" + str(authc_token) + "]")
        logger.debug(msg)

        if authc_token.TIER != 1:
            if not identifiers:
                msg = "Authentication must be performed in expected sequence."
                raise InvalidAuthenticationSequenceException(msg)
            authc_token.identifier = identifiers.primary_identifier

        try:
            account = self.do_authenticate_account(authc_token)
            if (account is None):
                msg2 = ("No account returned by any configured realms for "
                        "submitted authentication token [{0}]".
                        format(authc_token))

                raise UnknownAccountException(msg2)

        except AdditionalAuthenticationRequired as exc:
            self.notify_progress(authc_token)
            try:
                self.mfa_challenger.send_challenge(account)
            except AttributeError:
                pass
            raise  # the security_manager saves subject identifiers

        except AuthenticationException as account:
            self.notify_failure(authc_token)
            self.validate_locked(authc_token, account)
            raise  # this won't be called if the Account is locked

        self.notify_success(account)

        return account

    def do_authenticate_account(self, authc_token):
        """
        Returns an account object only when the current token authenticates AND
        the authentication process is complete, raising otherwise

        :returns:  Account
        :raises AdditionalAuthenticationRequired: when additional tokens are required,
                                                  passing the account object
        """
        realms = self.token_realm_resolver[authc_token.__class__]

        if (len(self.realms) == 1):
            account = self.authenticate_single_realm_account(realms[0], authc_token)

        else:
            account = self.authenticate_multi_realm_account(self.realms, authc_token)

        # required_authc_tokens is a list of strings of the token class names
        # such as ['TOTPToken', 'UsernamePasswordToken']
        if len(account.required_authc_tokens) > authc_token.tier:
            # the token authenticated but additional authentication is required
            self.notify_progress(authc_token)
            raise AdditionalAuthenticationRequired(account)

        return account
    # --------------------------------------------------------------------------
    # Event Communication
    # --------------------------------------------------------------------------

    def clear_cache(self, items=None):
        """
        expects event object to be in the format of a session-stop or
        session-expire event, whose results attribute is a
        namedtuple(identifiers, session_key)
        """
        try:
            for realm in self.realms:
                identifiers = items.identifiers
                identifier = identifiers.from_source(realm.name)
                if identifier:
                    realm.clear_cached_credentials(identifier)
        except AttributeError:
            msg = ('Could not clear authc_info from cache after event. '
                   'items: ' + str(items))
            logger.warn(msg)

    def register_cache_clear_listener(self):
        if self.event_bus:
            self.event_bus.register(self.clear_cache, 'SESSION.EXPIRE')
            self.event_bus.is_registered(self.clear_cache, 'SESSION.EXPIRE')
            self.event_bus.register(self.clear_cache, 'SESSION.STOP')
            self.event_bus.is_registered(self.clear_cache, 'SESSION.STOP')

    def notify_locked(self, identifier):
        try:
            self.event_bus.publish('AUTHENTICATION.ACCOUNT_LOCKED',
                                   identifier=identifier)
        except AttributeError:
            msg = "Could not publish AUTHENTICATION.ACCOUNT_LOCKED event"
            raise AuthenticationEventException(msg)

    def notify_progress(self, authc_token):
        try:
            self.event_bus.publish('AUTHENTICATION.PROGRESS',
                                   identifier=authc_token.identifier,
                                   token=authc_token.__class__.__name__)
        except AttributeError:
            msg = "Could not publish AUTHENTICATION.PROGRESS event"
            raise AuthenticationEventException(msg)

    def notify_success(self, account):
        try:
            self.event_bus.publish('AUTHENTICATION.SUCCEEDED',
                                   identifiers=account.account_id)
        except AttributeError:
            msg = "Could not publish AUTHENTICATION.SUCCEEDED event"
            raise AuthenticationEventException(msg)

    def notify_failure(self, authc_token):
        try:
            self.event_bus.publish('AUTHENTICATION.FAILED',
                                   username=authc_token.username)
        except AttributeError:
            msg = "Could not publish AUTHENTICATION.FAILED event"
            raise AuthenticationEventException(msg)

    def validate_locked(self, authc_token, account):
        token = authc_token.__class__.__name__
        failed_attempts = account.authc_info[token]['failed_attempts']

        if self.locking_limit:
            if len(failed_attempts) > self.locking_limit:
                self.locking_realm.lock_account(account)
                msg = ('Authentication attempts breached threshold.  Account'
                       'is now locked: ', str(account))
                self.notify_locked(account.account_id)
                raise LockedAccountException(msg)

    # --------------------------------------------------------------------------

    def __repr__(self):
        return "<DefaultAuthenticator(event_bus={0}, strategy={0})>".\
            format(self.event_bus, self.authentication_strategy)


class Credential(serialize_abcs.Serializable):

    def __init__(self, credential):
        """
        :type credential: bytestring
        """
        self.credential = credential

    def __eq__(self, other):
        return self.credential == other.credential

    def __bool__(self):
        return bool(self.credential)

    def __getstate__(self):
        return {'credential': self.credential}

    def __setstate__(self, state):
        self.credential = state['credential']
