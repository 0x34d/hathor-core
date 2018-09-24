from hathor.wallet.resources import AuthWalletResource
from twisted.internet.defer import inlineCallbacks
from tests.resources.base_resource import TestSite, _BaseResourceTest


class AuthTest(_BaseResourceTest._ResourceTest):
    def setUp(self):
        super().setUp()
        self.web = TestSite(AuthWalletResource(self.manager))

    @inlineCallbacks
    def test_unlocking(self):
        # Wallet is locked
        response = yield self.web.get("wallet/auth")
        data = response.json_value()
        self.assertTrue(data['is_locked'])

        # Try to unlock with wrong password
        response_error = yield self.web.post("wallet/auth", {b'password': b'wrong_password'})
        data_error = response_error.json_value()
        self.assertFalse(data_error['success'])

        # Try to unlock with correct password
        response_success = yield self.web.post("wallet/auth", {b'password': b'MYPASS'})
        data_success = response_success.json_value()
        self.assertTrue(data_success['success'])

        # Wallet is unlocked
        response_unlocked = yield self.web.get("wallet/auth")
        data_unlocked = response_unlocked.json_value()
        self.assertFalse(data_unlocked['is_locked'])
