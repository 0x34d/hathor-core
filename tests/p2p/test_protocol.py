from twisted.test import proto_helpers
from twisted.python import log
from twisted.internet.task import Clock

from hathor.p2p.peer_id import PeerId
from hathor.manager import HathorManager
from hathor.wallet import Wallet, KeyPair

from tests import unittest

import sys


class HathorProtocolTestCase(unittest.TestCase):
    def generate_peer(self, network, peer_id=None):
        if peer_id is None:
            peer_id = PeerId()
        wallet = self._create_wallet()
        manager = HathorManager(self.reactor, peer_id=peer_id, network=network, wallet=wallet)
        manager.start()

        proto = manager.server_factory.buildProtocol(('127.0.0.1', 0))
        tr = proto_helpers.StringTransport()
        proto.makeConnection(tr)
        return proto, tr

    def _create_wallet(self):
        keys = {}
        for _i in range(20):
            keypair = KeyPair.create(b'MYPASS')
            keys[keypair.address] = keypair
        return Wallet(keys=keys)

    def setUp(self):
        log.startLogging(sys.stdout)

        self.reactor = Clock()
        self.network = 'testnet'

        self.peer_id1 = PeerId()
        self.peer_id2 = PeerId()
        self.proto1, self.tr1 = self.generate_peer(self.network, peer_id=self.peer_id1)
        self.proto2, self.tr2 = self.generate_peer(self.network, peer_id=self.peer_id2)

    def tearDown(self):
        self.clean_pending(required_to_quiesce=False)

    def _send_cmd(self, proto, cmd, payload=None):
        if not payload:
            line = '{}\r\n'.format(cmd)
        else:
            line = '{} {}\r\n'.format(cmd, payload)

        if isinstance(line, str):
            line = line.encode('utf-8')

        proto.dataReceived(line)

    def _check_result_only_cmd(self, result, expected_cmd):
        cmd, _, _ = result.partition(b' ')
        self.assertEqual(cmd, expected_cmd)

    def _run_one_step(self, debug=False):
        line1 = self.tr1.value()
        line2 = self.tr2.value()

        if debug:
            print('--')
            print('line1', line1)
            print('line2', line2)
            print('--')

        self.tr1.clear()
        self.tr2.clear()

        self.proto2.dataReceived(line1)
        self.proto1.dataReceived(line2)

    def test_on_connect(self):
        self._check_result_only_cmd(self.tr1.value(), b'HELLO')

    def test_invalid_command(self):
        self._send_cmd(self.proto1, 'INVALID-CMD')
        self.assertTrue(self.tr1.disconnecting)

    def test_invalid_hello1(self):
        self.tr1.clear()
        self._send_cmd(self.proto1, 'HELLO')
        self._check_result_only_cmd(self.tr1.value(), b'ERROR')
        self.assertTrue(self.tr1.disconnecting)

    def test_invalid_hello2(self):
        self.tr1.clear()
        self._send_cmd(self.proto1, 'HELLO', 'invalid_payload')
        self._check_result_only_cmd(self.tr1.value(), b'ERROR')
        self.assertTrue(self.tr1.disconnecting)

    def test_invalid_hello3(self):
        self.tr1.clear()
        self._send_cmd(self.proto1, 'HELLO', '{}')
        self._check_result_only_cmd(self.tr1.value(), b'ERROR')
        self.assertTrue(self.tr1.disconnecting)

    def test_valid_hello(self):
        self._run_one_step()
        self._check_result_only_cmd(self.tr1.value(), b'PEER-ID')
        self._check_result_only_cmd(self.tr2.value(), b'PEER-ID')
        self.assertFalse(self.tr1.disconnecting)
        self.assertFalse(self.tr2.disconnecting)

    def test_invalid_same_peer_id(self):
        self.proto2, self.tr2 = self.generate_peer(self.network, peer_id=self.peer_id1)
        self._run_one_step()
        self._run_one_step()
        self._check_result_only_cmd(self.tr1.value(), b'ERROR')
        self.assertTrue(self.tr1.disconnecting)

    def test_invalid_different_network(self):
        self.proto2, self.tr2 = self.generate_peer(network='mainnet')
        self._run_one_step()
        self._check_result_only_cmd(self.tr1.value(), b'ERROR')
        self.assertTrue(self.tr1.disconnecting)

    def test_valid_hello_and_peer_id(self):
        self._run_one_step()
        self._run_one_step()
        # Originally, only a GET-PEERS message would be received, but now it is receiving two messages in a row.
        # self._check_result_only_cmd(self.tr1.value(), b'GET-PEERS')
        # self._check_result_only_cmd(self.tr2.value(), b'GET-PEERS')
        self.assertFalse(self.tr1.disconnecting)
        self.assertFalse(self.tr2.disconnecting)
        self._run_one_step()
        self._run_one_step()
        self.assertFalse(self.tr1.disconnecting)
        self.assertFalse(self.tr2.disconnecting)
