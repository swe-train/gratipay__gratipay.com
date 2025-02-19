"""
"""
from __future__ import absolute_import, division, print_function, unicode_literals

from datetime import timedelta
import json
from urlparse import urlsplit, urlunsplit
import uuid
import xml.etree.ElementTree as ET

from aspen import Response
from aspen.utils import utcnow
from postgres.orm import Model
from psycopg2 import IntegrityError
import xmltodict

import gratipay
from gratipay.exceptions import ProblemWithUsername
from gratipay.security.crypto import constant_time_compare
from gratipay.utils.username import safely_reserve_a_username


CONNECT_TOKEN_TIMEOUT = timedelta(hours=24)


class UnknownAccountElsewhere(Exception): pass


class AccountElsewhere(Model):
    """Participants can attach accounts on other platforms (Facebook, Twitter,
    etc.) to their Gratipay account.
    """

    typname = "elsewhere_with_participant"

    def __init__(self, *args, **kwargs):
        super(AccountElsewhere, self).__init__(*args, **kwargs)
        self.platform_data = getattr(self.platforms, self.platform)


    # Constructors
    # ============

    @classmethod
    def from_id(cls, id):
        """Return an existing AccountElsewhere based on id.
        """
        return cls.db.one("""
            SELECT elsewhere.*::elsewhere_with_participant
              FROM elsewhere
             WHERE id = %s
        """, (id,))

    @classmethod
    def from_user_id(cls, platform, user_id):
        """Return an existing AccountElsewhere based on platform and user_id.
        """
        return cls._from_thing('user_id', platform, user_id)

    @classmethod
    def from_user_name(cls, platform, user_name):
        """Return an existing AccountElsewhere based on platform and user_name.
        """
        return cls._from_thing('user_name', platform, user_name)

    @classmethod
    def _from_thing(cls, thing, platform, value):
        assert thing in ('user_id', 'user_name')
        exception = UnknownAccountElsewhere(thing, platform, value)
        return cls.db.one("""

            SELECT elsewhere.*::elsewhere_with_participant
              FROM elsewhere
             WHERE platform = %s
               AND {} = %s

        """.format(thing), (platform, value), default=exception)

    @classmethod
    def get_many(cls, platform, user_infos):
        accounts = []
        found = cls.db.all("""\

            SELECT elsewhere.*::elsewhere_with_participant
              FROM elsewhere
             WHERE platform = %s
               AND user_id = any(%s)

        """, (platform, [i.user_id for i in user_infos]))
        found = {a.user_id: a for a in found}
        for i in user_infos:
            if i.user_id in found:
                accounts.append(found[i.user_id])
            else:
                accounts.append(cls.upsert(i))
        return accounts

    @classmethod
    def upsert(cls, i):
        """Insert or update a user's info.
        """

        # Clean up avatar_url
        if i.avatar_url:
            scheme, netloc, path, query, fragment = urlsplit(i.avatar_url)
            fragment = ''
            if netloc.endswith('githubusercontent.com') or \
               netloc.endswith('gravatar.com'):
                query = 's=160'
            i.avatar_url = urlunsplit((scheme, netloc, path, query, fragment))

        # Serialize extra_info
        if isinstance(i.extra_info, ET.Element):
            i.extra_info = xmltodict.parse(ET.tostring(i.extra_info))
        i.extra_info = json.dumps(i.extra_info)

        cols, vals = zip(*i.__dict__.items())
        cols = ', '.join(cols)
        placeholders = ', '.join(['%s']*len(vals))

        try:
            # Try to insert the account
            # We do this with a transaction so that if the insert fails, the
            # participant we reserved for them is rolled back as well.
            with cls.db.get_cursor() as cursor:
                username = safely_reserve_a_username(cursor)
                cursor.execute("""
                    INSERT INTO elsewhere
                                (participant, {0})
                         VALUES (%s, {1})
                """.format(cols, placeholders), (username,)+vals)
        except IntegrityError:
            # The account is already in the DB, update it instead
            username = cls.db.one("""
                UPDATE elsewhere
                   SET ({0}) = ({1})
                 WHERE platform=%s AND user_id=%s
             RETURNING participant
            """.format(cols, placeholders), vals+(i.platform, i.user_id))
            if not username:
                raise

        # Return account after propagating avatar_url to participant
        account = AccountElsewhere.from_user_id(i.platform, i.user_id)
        account.participant.update_avatar()
        return account


    # Connect tokens
    # ==============

    def check_connect_token(self, token):
        return (
            self.connect_token and
            constant_time_compare(self.connect_token, token) and
            self.connect_expires > utcnow()
        )

    def make_connect_token(self):
        token = uuid.uuid4().hex
        expires = utcnow() + CONNECT_TOKEN_TIMEOUT
        return self.save_connect_token(token, expires)

    def save_connect_token(self, token, expires):
        return self.db.one("""
            UPDATE elsewhere
               SET connect_token = %s
                 , connect_expires = %s
             WHERE id = %s
         RETURNING connect_token, connect_expires
        """, (token, expires, self.id))


    # Random Stuff
    # ============

    def get_auth_session(self):
        if not self.token:
            return
        params = dict(token=self.token)
        if 'refresh_token' in self.token:
            params['token_updater'] = self.save_token
        return self.platform_data.get_auth_session(**params)

    @property
    def gratipay_slug(self):
        return self.user_name or ('~' + self.user_id)

    @property
    def gratipay_url(self):
        base_url = gratipay.base_url
        platform = self.platform
        slug = self.gratipay_slug
        return "{base_url}/on/{platform}/{slug}/".format(**locals())

    @property
    def html_url(self):
        return self.platform_data.account_url.format(
            user_id=self.user_id,
            user_name=self.user_name,
            platform_data=self.platform_data
        )

    @property
    def friendly_name(self):
        if getattr(self.platform, 'optional_user_name', False):
            return self.display_name or self.user_name or self.user_id
        else:
            return self.user_name or self.display_name or self.user_id

    @property
    def friendly_name_long(self):
        r = self.friendly_name
        display_name = self.display_name
        if display_name and display_name != r:
            return '%s (%s)' % (r, display_name)
        user_name = self.user_name
        if user_name and user_name != r:
            return '%s (%s)' % (r, user_name)
        return r

    def opt_in(self, desired_username):
        """Given a desired username, return a User object.
        """
        from gratipay.security.user import User
        user = User.from_username(self.participant.username)
        assert not user.ANON, self.participant  # sanity check
        if self.participant.is_claimed:
            newly_claimed = False
        else:
            newly_claimed = True
            user.participant.set_as_claimed()
            try:
                user.participant.change_username(desired_username)
            except ProblemWithUsername:
                pass
        if user.participant.is_closed:
            user.participant.update_is_closed(False)
        return user, newly_claimed

    def save_token(self, token):
        """Saves the given access token in the database.
        """
        self.db.run("""
            UPDATE elsewhere
               SET token = %s
             WHERE id=%s
        """, (token, self.id))
        self.set_attributes(token=token)


def get_account_elsewhere(website, state, api_lookup=True):
    _ = state['_']
    path = state['request'].line.uri.path

    platform = getattr(website.platforms, path['platform'], None)
    if platform is None:
        raise Response(404)

    uid = path['user_name']
    if "\t" in uid or "\r" in uid or "\n" in uid:
        raise Response(400, _("Invalid character in elsewhere account username."))
    if uid[:1] == '~':
        key = 'user_id'
        uid = uid[1:]
    else:
        key = 'user_name'
    try:
        account = AccountElsewhere._from_thing(key, platform.name, uid)
    except UnknownAccountElsewhere:
        account = None
    if not account:
        if not api_lookup:
            raise Response(404)
        try:
            user_info = platform.get_user_info(key, uid)
        except Response as r:
            if r.code == 404:
                err = _("Account not found on {0}.", platform.display_name)
                raise Response(404, err)
            raise
        account = AccountElsewhere.upsert(user_info)
    return platform, account
