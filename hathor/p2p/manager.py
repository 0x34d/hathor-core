# encoding: utf-8

from twisted.internet import reactor, endpoints
from twisted.internet.task import LoopingCall
import twisted.names.client

from hathor.p2p.peer_id import PeerId
from hathor.p2p.peer_storage import PeerStorage
from hathor.p2p.factory import HathorServerFactory, HathorClientFactory
from hathor.p2p.node_sync import NodeSyncLeftToRightManager
from hathor.transaction import Block, TxOutput
from hathor.transaction.storage.memory_storage import TransactionMemoryStorage
from hathor.crypto.util import generate_privkey_crt_pem
from hathor.pubsub import HathorEvents, PubSubManager
from hathor.exception import HathorError

from collections import defaultdict, deque
from enum import Enum
from math import log
import time
import socket
import random

from hathor.p2p.protocol import HathorLineReceiver
MyServerProtocol = HathorLineReceiver
MyClientProtocol = HathorLineReceiver

# from hathor.p2p.protocol import HathorWebSocketServerProtocol, HathorWebSocketClientProtocol
# MyServerProtocol = HathorWebSocketServerProtocol
# MyClientProtocol = HathorWebSocketClientProtocol


class HathorManager(object):
    """ HathorManager manages the node, including bootstraping, peer discovery, connections,
    synchonization, and so on.
    """

    class NodeState(Enum):
        INITIALIZING = 'INITIALIZING'  # This node is still initializing
        WAITING_FOR_PEERS = 'WAITING_FOR_PEERS'
        SYNCING = 'SYNCING'  # This node is still synchronizing with the network
        SYNCED = 'SYNCED'    # This node is up-to-date with the network

    def __init__(self, server_factory=None, client_factory=None, peer_id=None, network=None, hostname=None,
                 pubsub=None, wallet=None, tx_storage=None, peer_storage=None, default_port=40403):
        """
        :param server_factory: Factory used when new connections arrive.
        :type server_factory: :py:class:`hathor.p2p.factory.HathorServerFactory`

        :param client_factory: Factory used when opening new connections.
        :type client_factory: :py:class:`hathor.p2p.factory.HathorClientFactory`

        :param peer_id: Id of this node. If not given, a new one is created.
        :type peer_id: :py:class:`hathor.p2p.peer_id.PeerId`

        :param network: Name of the network this node participates. Usually it is either testnet or mainnet.
        :type network: string

        :param hostname: The hostname of this node. It is used to generate its entrypoints.
        :type hostname: string

        :param pubsub: If not given, a new one is created.
        :type pubsub: :py:class:`hathor.pubsub.PubSubManager`

        :param tx_storage: If not given, a :py:class:`TransactionMemoryStorage` one is created.
        :type tx_storage: :py:class:`hathor.transaction.storage.transaction_storage.TransactionStorage`

        :param peer_storage: If not given, a new one is created.
        :type peer_storage: :py:class:`hathor.p2p.peer_storage.PeerStorage`

        :param default_port: Network default port. It is used when only ip addresses are discovered.
        :type default_port: int
        """
        self.state = None

        # Factories.
        self.server_factory = server_factory or HathorServerFactory()
        self.server_factory.manager = self

        self.client_factory = client_factory or HathorClientFactory()
        self.client_factory.manager = self

        # Hostname, used to be accessed by other peers.
        self.hostname = hostname

        # Remote address, which can be different from local address.
        self.remote_address = None

        # XXX Should we use a singleton or a new PeerStorage? [msbrogli 2018-08-29]
        self.peer_storage = peer_storage or PeerStorage()
        self.tx_storage = tx_storage or TransactionMemoryStorage()
        self.pubsub = pubsub or PubSubManager()

        # Map of peer_id to the best block height reported by that peer.
        self.peer_best_heights = defaultdict(int)

        self.node_sync_manager = NodeSyncLeftToRightManager(self)
        self.wallet = wallet
        self.wallet.pubsub = self.pubsub

        self.my_peer = peer_id or PeerId()
        self.network = network or 'testnet'
        self.default_port = default_port

        self.blocks_per_difficulty = 5
        self.latest_blocks = deque()
        self.avg_time_between_blocks = 64  # in seconds
        self.min_block_weight = 10
        self.block_weight = 10  # starting difficulty (10 is too low for production)
        self.max_allowed_block_weight_change = 2
        self.tokens_issued_per_block = 10000

        # A timer to try to reconnect to the disconnect known peers.
        self.lc_reconnect = LoopingCall(self.reconnect_to_all)

    def doStart(self):
        """ A factory must be started only once. And it is usually automatically started.
        """
        self.state = self.NodeState.INITIALIZING
        self.pubsub.publish(HathorEvents.MANAGER_ON_START)

        # Initialize manager's components.
        self._initialize_components()

        # List of pending connections.
        self.connecting_peers = {}  # Dict[IStreamClientEndpoint, twisted.internet.defer.Deferred]

        # List of peers connected but still not ready to communicate.
        self.handshaking_peers = set()  # Set[HathorProtocol]

        # List of peers connected and ready to communicate.
        self.connected_peers = {}  # Dict[string (peer.id), HathorProtocol]

        self.start_time = time.time()
        self.lc_reconnect.start(5)

    def _initialize_components(self):
        """You are not supposed to run this method manually. You should run `doStart()` to initialize the
        manager.

        This method runs through all transactions, verifying them and updating our wallet.
        """
        if self.wallet:
            self.wallet._manually_initialize()
        for tx in self.tx_storage._topological_sort():
            self.on_new_tx(tx)
        self.state = self.NodeState.WAITING_FOR_PEERS

    def doStop(self):
        self.pubsub.publish(HathorEvents.MANAGER_ON_STOP)
        self.lc_reconnect.stop()

    def on_connection_failure(self, failure, endpoint):
        print('Connection failure: address={}:{} message={}'.format(endpoint._host, endpoint._port, failure))
        self.connecting_peers.pop(endpoint)

    def on_peer_connect(self, protocol):
        print('on_peer_connect()', protocol)
        self.handshaking_peers.add(protocol)

    def on_peer_ready(self, protocol):
        print('on_peer_ready()', protocol)
        self.handshaking_peers.remove(protocol)
        self.peer_storage.add_or_merge(protocol.peer)
        self.connected_peers[protocol.peer.id] = protocol

    def on_peer_disconnect(self, protocol):
        print('on_peer_disconnect()', protocol)
        if protocol.peer:
            self.connected_peers.pop(protocol.peer.id)
        if protocol in self.handshaking_peers:
            self.handshaking_peers.remove(protocol)

    def propagate_tx(self, tx):
        """Push a new transaction to the network. It is used by both the wallet and the mining modules.
        """
        if tx.storage:
            assert tx.storage == self.tx_storage, 'Invalid tx storage'
        else:
            tx.storage = self.tx_storage
        self.on_new_tx(tx)

        # Only propagate transactions once we are sufficiently synced up with the rest of the network.
        if self.state == self.NodeState.SYNCED:
            for conn in self.connected_peers.values():
                conn.state.send_data(tx)

    def get_new_tx_parents(self):
        """Select which transactions will be confirmed by a new transaction.

        :return: The hashes of the parents for a new transaction.
        :rtype: List[bytes(hash)]
        """
        tips = self.tx_storage.get_tip_transactions(count=2)
        ret = [x.hash for x in tips]
        if len(tips) == 1:
            # If there is only one tip, let's randomly choose one of its parents.
            ret.append(random.choice(tips[0].parents))
        return ret

    def generate_mining_block(self):
        address = self.wallet.get_unused_address_bytes(mark_as_used=False)
        amount = self.tokens_issued_per_block
        tx_outputs = [
            TxOutput(amount, address)
        ]
        tip_blocks = self.tx_storage.get_tip_blocks_hashes()
        tip_txs = self.get_new_tx_parents()
        parents = tip_blocks + tip_txs

        parents_tx = [self.tx_storage.get_transaction_by_hash_bytes(x) for x in parents]
        new_height = max(x.height for x in parents_tx) + 1

        return Block(weight=self.block_weight, outputs=tx_outputs, parents=parents, storage=self.tx_storage,
                     height=new_height)

    def on_tips_received(self, tip_blocks, tip_transactions, conn=None):
        self.node_sync_manager.on_tips_received(tip_blocks, tip_transactions, conn)

    def validate_new_tx(self, tx):
        """ Process incoming transaction during initialization.
        These transactions came only from storage.
        """
        if self.state != self.NodeState.INITIALIZING:
            if tx.is_genesis:
                print('validate_new_tx(): Genesis? {}'.format(tx.hash.hex()))
                return False

            if self.tx_storage.transaction_exists_by_hash_bytes(tx.hash):
                print('validate_new_tx(): Already have transaction {}'.format(tx.hash.hex()))
                return False

        for parent_hash in tx.parents:
            if not self.tx_storage.transaction_exists_by_hash_bytes(parent_hash):
                # All parents must exist.
                print('validate_new_tx(): Invalid transaction with unknown parent tx={} parent={}'.format(
                    tx.hash.hex(), parent_hash.hex()
                ))
                return False

        try:
            tx.verify()
        except HathorError as e:
            print('validate_new_tx(): Error verifying transaction {} tx={}'.format(e, tx.hash.hex()))
            return False

        if tx.is_block:
            if tx.weight < self.block_weight:
                print('Invalid new block {}: weight ({}) is smaller than the minimum block weight ({})'.format(
                    tx.hash.hex(), tx.weight, self.block_weight)
                )
                return False
            if tx.sum_outputs != self.tokens_issued_per_block:
                print('Invalid number of issued tokens: {} <> {} (tx: {})'.format(
                    tx.sum_outputs,
                    self.tokens_issued_per_block,
                    tx.hash.hex())
                )

        return True

    def on_new_tx(self, tx, conn=None):
        """This method is called when any transaction arrive.
        """
        if not self.validate_new_tx(tx):
            # Discard invalid Transaction/block.
            return

        if self.wallet:
            self.wallet.on_new_tx(tx)

        if self.state == self.NodeState.INITIALIZING:
            self.tx_storage._add_to_cache(tx)
        else:
            self.tx_storage.save_transaction(tx)
            self.node_sync_manager.on_new_tx(tx, conn)

        if tx.is_block:
            self.latest_blocks.append(tx)
            while len(self.latest_blocks) > self.blocks_per_difficulty:
                self.latest_blocks.popleft()

            print('New block found: {} weight={}'.format(tx.hash_hex, tx.weight))
            count_blocks = self.tx_storage.get_block_count()
            if count_blocks % self.blocks_per_difficulty == 0:
                print('Adjusting mining difficulty...')
                avg_dt, new_weight = self.calculate_block_difficulty()
                print('Block weight updated: avg_dt={:.2f} target_avg_dt={:.2f} {:6.2f} -> {:6.2f}'.format(
                    avg_dt,
                    self.avg_time_between_blocks,
                    self.block_weight,
                    new_weight
                ))
                self.block_weight = new_weight
        else:
            print('New tx: {}'.format(tx.hash.hex()))

    def on_block_hashes_received(self, block_hashes, conn=None):
        """We have received a list of hashes of blocks, according to a peer."""
        self.node_sync_manager.on_block_hashes_received(block_hashes, conn)

    def on_transactions_hashes_received(self, txs_hashes, conn=None):
        """We have received a list of hashes of transactions, according to a peer."""
        self.node_sync_manager.on_transactions_hashes_received(txs_hashes, conn)

    def on_best_height(self, best_height, conn):
        raise NotImplemented

    def calculate_block_difficulty(self):
        blocks = self.latest_blocks
        assert len(blocks) == self.blocks_per_difficulty
        dt = blocks[-1].timestamp - blocks[0].timestamp

        if dt <= 0:
            dt = 1  # Strange situation, so, let's just increase difficulty.

        delta = (
            log(self.avg_time_between_blocks, 2)
            + log(self.blocks_per_difficulty, 2)
            - log(dt, 2)
        )

        if delta > self.max_allowed_block_weight_change:
            delta = self.max_allowed_block_weight_change
        elif delta < -self.max_allowed_block_weight_change:
            delta = -self.max_allowed_block_weight_change

        new_weight = self.block_weight + delta

        if new_weight < self.min_block_weight:
            new_weight = self.min_block_weight

        avg_dt = float(dt) / self.blocks_per_difficulty
        return avg_dt, new_weight

    def update_peer(self, peer):
        """ Update a peer information in our storage, and instantly attempt to connect
        to it if it is not connected yet.
        """
        if peer.id == self.my_peer.id:
            return
        self.peer_storage.add_or_merge(peer)
        self.connect_to_if_not_connected(peer)

    def reconnect_to_all(self):
        """ It is called by the `lc_reconnect` timer and tries to connect to all known
        peers.

        TODO(epnichols): Should we always conect to *all*? Should there be a max #?
        """
        for peer in self.peer_storage.values():
            self.connect_to_if_not_connected(peer)

    def connect_to_if_not_connected(self, peer):
        """ Attempts to connect if it is not connected to the peer.
        """
        if not peer.entrypoints:
            return
        if peer.id not in self.connected_peers:
            self.connect_to(random.choice(peer.entrypoints))

    def _connect_to_callback(self, protocol, endpoint):
        self.connecting_peers.pop(endpoint)

    def connect_to(self, description, ssl=False):
        """ Attempt to connect to a peer, even if a connection already exists.
        Usually you should call `connect_to_if_not_connected`.

        If `ssl` is True, then the connection will be wraped by a TLS.
        """
        endpoint = self.clientFromString(description)

        if ssl:
            from twisted.internet import ssl
            from twisted.protocols.tls import TLSMemoryBIOFactory
            context = ssl.ClientContextFactory()
            factory = TLSMemoryBIOFactory(context, True, self.client_factory)
        else:
            factory = self.server_factory

        deferred = endpoint.connect(factory)
        self.connecting_peers[endpoint] = deferred

        deferred.addCallback(self._connect_to_callback, endpoint)
        deferred.addErrback(self.on_connection_failure, endpoint)
        print('Connecting to: {}...'.format(description))

    def serverFromString(self, description):
        """ Return an endpoint which will be used to listen to new connection.
        """
        return endpoints.serverFromString(reactor, description)

    def listen(self, description, ssl=False):
        """ Start to listen to new connection according to the description.

        If `ssl` is True, then the connection will be wraped by a TLS.

        :Example:

        `manager.listen(description='tcp:8000')`

        :param description: A description of the protocol and its parameters.
        :type description: str
        """
        endpoint = self.serverFromString(description)

        if ssl:
            # XXX Is it safe to generate a new certificate for each connection?
            #     What about CPU usage when many new connections arrive?
            from twisted.internet.ssl import PrivateCertificate
            from twisted.protocols.tls import TLSMemoryBIOFactory
            certificate = PrivateCertificate.loadPEM(generate_privkey_crt_pem())
            contextFactory = certificate.options()
            factory = TLSMemoryBIOFactory(contextFactory, False, self.server_factory)

            # from twisted.internet.ssl import CertificateOptions, TLSVersion
            # options = dict(privateKey=certificate.privateKey.original, certificate=certificate.original)
            # contextFactory = CertificateOptions(
            #     insecurelyLowerMinimumTo=TLSVersion.TLSv1_2,
            #     lowerMaximumSecurityTo=TLSVersion.TLSv1_3,
            #     **options,
            # )
        else:
            factory = self.client_factory

        endpoint.listen(factory)
        print('Listening to: {}...'.format(description))
        if self.hostname:
            proto, _, _ = description.partition(':')
            address = '{}:{}:{}'.format(proto, self.hostname, endpoint._port)
            self.my_peer.entrypoints.append(address)

    def dns_seed_lookup_text(self, host):
        """ Run a DNS lookup for TXT records to discover new peers.
        """
        x = twisted.names.client.lookupText(host)
        x.addCallback(self.on_dns_seed_found)

    def dns_seed_lookup_address(self, host):
        """ Run a DNS lookup for A records to discover new peers.
        """
        x = twisted.names.client.lookupAddress(host)
        x.addCallback(self.on_dns_seed_found_ipv4)

    def dns_seed_lookup_ipv6_address(self, host):
        """ Run a DNS lookup for AAAA records to discover new peers.
        """
        x = twisted.names.client.lookupIPV6Address(host)
        x.addCallback(self.on_dns_seed_found_ipv6)

    def dns_seed_lookup(self, host):
        """ Run a DNS lookup for TXT, A, and AAAA records to discover new peers.
        """
        self.dns_seed_lookup_text(host)
        self.dns_seed_lookup_address(host)
        # self.dns_seed_lookup_ipv6_address(host)

    def clientFromString(self, description):
        """ Return an endpoint which will be used to open a new connection.
        """
        return endpoints.clientFromString(reactor, description)

    def on_dns_seed_found(self, results):
        """ Executed only when a new peer is discovered by `dns_seed_lookup_text`.
        """
        answers, _, _ = results
        for x in answers:
            data = x.payload.data
            for txt in data:
                txt = txt.decode('utf-8')
                try:
                    print('Seed DNS TXT: "{}" found'.format(txt))
                    self.connect_to(txt)
                except ValueError:
                    print('Seed DNS TXT: Error parsing "{}"'.format(txt))

    def on_dns_seed_found_ipv4(self, results):
        """ Executed only when a new peer is discovered by `dns_seed_lookup_address`.
        """
        answers, _, _ = results
        for x in answers:
            address = x.payload.address
            host = socket.inet_ntoa(address)
            self.connect_to('tcp:{}:{}'.format(host, self.default_port))
            print('Seed DNS A: "{}" found'.format(host))

    def on_dns_seed_found_ipv6(self, results):
        """ Executed only when a new peer is discovered by `dns_seed_lookup_ipv6_address`.
        """
        # answers, _, _ = results
        # for x in answers:
        #     address = x.payload.address
        #     host = socket.inet_ntop(socket.AF_INET6, address)
        raise NotImplemented()
