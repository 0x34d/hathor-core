from twisted.internet.defer import inlineCallbacks

from hathor.transaction.resources import ValidateAddressResource
from tests.resources.base_resource import StubSite, _BaseResourceTest


class TransactionTest(_BaseResourceTest._ResourceTest):
    def setUp(self):
        super().setUp()
        self.web = StubSite(ValidateAddressResource(self.manager))

    # Example from the design:
    #
    # ❯ curl localhost:9080/v1a/validate_address/HNXsVtRUmwDCtpcCJUrH4QiHo9kUKx199A -s | jq
    # {
    #   "valid": true,
    #   "script": "dqkUr6YAVWv0Ps6bjgSGuqMb1GqCw6+IrA==",
    #   "address": "HNXsVtRUmwDCtpcCJUrH4QiHo9kUKx199A",
    #   "type": "p2pkh"
    # }

    @inlineCallbacks
    def test_simple(self):
        address = 'HNXsVtRUmwDCtpcCJUrH4QiHo9kUKx199A'
        response_success = yield self.web.get(address)
        data_success = response_success.json_value()
        self.assertEqual(data_success, {
           'valid': True,
           'script': 'dqkUr6YAVWv0Ps6bjgSGuqMb1GqCw6+IrA==',
           'address': address,
           'type': 'p2pkh',
        })

    @inlineCallbacks
    def test_invalid_network(self):
        # this address is valid on the testnet
        response_success = yield self.web.get('WTPcVyGjo9tSet8QAH7qudW2LwtkgubZGU')
        data_success = response_success.json_value()
        self.assertEqual(data_success, {
           'valid': False,
           'error': 'ScriptError',
           'msg': 'The address is not valid',
        })

    @inlineCallbacks
    def test_wrong_size(self):
        address = 'HNXsVtRUmwDCtpcCJUrH4QiHo9kUKx199Aa'
        response_success = yield self.web.get(address)
        data_success = response_success.json_value()
        self.assertEqual(data_success, {
           'valid': False,
           'error': 'InvalidAddress',
           'msg': 'Address size must have 25 bytes',
        })

    @inlineCallbacks
    def test_gibberish(self):
        # this isn't remotely what an address looks like
        response_success = yield self.web.get('ahl8sfyoiuh23$%!!dfads')
        data_success = response_success.json_value()
        self.assertEqual(data_success, {
           'valid': False,
           'error': 'InvalidAddress',
           'msg': 'Invalid base58 address',
        })
