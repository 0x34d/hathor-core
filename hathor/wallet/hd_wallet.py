from mnemonic import Mnemonic
from ecdsa import SigningKey, SECP256k1
from pycoin.key.BIP32Node import BIP32Node
from pycoin.encoding import to_bytes_32
from hathor.wallet import BaseWallet
from hathor.crypto.util import get_private_key_from_bytes, get_address_b58_from_public_key_bytes_compressed
from hathor.pubsub import HathorEvents
from hathor.wallet.exceptions import InvalidWords

# TODO pycoin BIP32 uses their own ecdsa library to generate key that does not use OpenSSL
# We must check if this brings any security problem to us later

WORD_COUNT_CHOICES = [12, 15, 18, 21, 24]


class HDWallet(BaseWallet):
    """ Hierarchical Deterministic Wallet based in BIP32 (https://github.com/bitcoin/bips/blob/master/bip-0032.mediawiki)
    """
    def __init__(self, words=None, language='english', passphrase=b'', gap_limit=20,
                 word_count=24, directory='./', history_file='history.json', pubsub=None):
        """
        :param words: words to generate the seed. It's a string with the words separated by a single space.
        If None we generate new words when starting the wallet
        :type words: string

        :param language: language of the words
        :type language: string

        :param passphrase: one more security level to generate the seed
        :type passphrase: bytes

        :param gap_limit: maximum of unused addresses in sequence
        (default value based in https://github.com/bitcoin/bips/blob/master/bip-0044.mediawiki#address-gap-limit)
        :type gap_limit: int

        :param word_count: quantity of words that are gonna generate the seed
        Possible choices are [12, 15, 18, 21, 24]
        :type word_count: int

        :raises ValueError: Raised on invalid word_count
        """
        super().__init__(
            directory=directory,
            history_file=history_file,
            pubsub=pubsub,
        )

        # Dict[string(base58), BIP32Key]
        self.keys = {}

        # Last index that the address was shared
        # We use this index to know which address should be shared with the user
        # This index together with last_generated_index show us if the gap limit was achieved
        self.last_shared_index = 0

        # Last index that the address was generated
        self.last_generated_index = 0

        # Maximum gap between indexes of last generated address and last used address
        self.gap_limit = gap_limit

        # XXX Should we  save this data in the object?
        self.language = language
        self.words = words
        self.passphrase = passphrase
        self.mnemonic = None

        # Used in admin frontend to know which wallet is being used
        self.type = self.WalletType.HD

        # Validating word count
        if word_count not in WORD_COUNT_CHOICES:
            raise ValueError('Word count ({}) is not one of the options {}.'.format(word_count, WORD_COUNT_CHOICES))
        self.word_count = word_count

    def _manually_initialize(self):
        """ Create words (if is None) and start seed and master node
            Then we generate the first addresses, so we can check if we already have transactions
        """
        self.mnemonic = Mnemonic(self.language)

        if not self.words:
            # Initialized but still locked
            return

        # Validate words first
        self.validate_words()

        assert isinstance(self.passphrase, bytes), 'Passphrase must be in bytes'

        # Master seed
        seed = self.mnemonic.to_seed(self.words, self.passphrase.decode('utf-8'))

        # Master node
        key = BIP32Node.from_master_secret(seed)

        # Until account key should be hardened
        # Chain path = 44'/0'/0'/0
        # 44' (hardened) -> BIP44
        # 0' (hardened) -> Coin type (0 = bitcoin) TODO change to hathor
        # 0' (hardened) -> Account
        # 0 -> Chain
        self.chain_key = key.subkey_for_path('44H/0H/0H/0')

        for idx in range(self.gap_limit):
            self.generate_new_key(idx)

    def get_private_key(self, address58):
        """ We get the private key bytes and generate the cryptography object

            :param address58: address in base58
            :type address58: string

            :return: Private key object.
            :rtype: :py:class:`cryptography.hazmat.primitives.asymmetric.ec.EllipticCurvePrivateKey`
        """
        my_key = self.keys[address58]
        signing_key = SigningKey.from_string(to_bytes_32(my_key.secret_exponent()), curve=SECP256k1)
        return get_private_key_from_bytes(signing_key.to_der())

    def generate_new_key(self, index):
        """ Generate a new key in the tree at defined index
            We add this new key to self.keys and set last_generated_index

            :param index: index to generate the key
            :type index: int
        """
        new_key = self.chain_key.subkey(index)
        self.keys[self.get_address(new_key)] = new_key
        self.last_generated_index = index

    def get_address(self, new_key):
        # XXX Apparently we are defining the address differently from bitcoin
        # We are doing sha256 + ripe160 only without the network byte and the checksum
        # So I am using the same address algorithm as before
        return get_address_b58_from_public_key_bytes_compressed(new_key.sec())

    def get_key_at_index(self, index):
        """ Return the key generated by the index in the parameter

            :param index: index to return the key
            :type index: int
        """
        return self.chain_key.subkey(index)

    def tokens_received(self, address58):
        """ Method called when the wallet receive new tokens

            If the gap limit is not yet achieved we generate more keys

            :param address58: address that received the token in base58
            :type address58: string
        """
        received_key = self.keys[address58]

        # If the gap now is less than the limit, we generate the new keys until the limit
        # Because we might be in sync phase, so we need those keys pre generated
        diff = self.last_generated_index - received_key.child_index()
        if (self.gap_limit - diff) > 0:
            for _ in range(self.gap_limit - diff):
                self.generate_new_key(self.last_generated_index + 1)

        # Last shared index should be at least the index after the received one
        self.last_shared_index = max(self.last_shared_index, received_key.child_index() + 1)

    def get_unused_address(self, mark_as_used=True):
        """ Return an address that is not used yet

            :param mark_as_used: if True we consider that this address is already used
            :type mark_as_used: bool

            :return: unused address in base58
            :rtype: string
        """
        if self.last_shared_index != self.last_generated_index:
            # Only in case we are not yet in the gap limit
            if mark_as_used:
                self.last_shared_index += 1
        else:
            if mark_as_used:
                self.publish_update(HathorEvents.WALLET_GAP_LIMIT, limit=self.gap_limit)

        key = self.get_key_at_index(self.last_shared_index)
        return self.get_address(key)

    def is_locked(self):
        """ Return if wallet is currently locked
            The wallet is locked if self.words is None

            :return: if wallet is locked
            :rtype: bool
        """
        return self.words is None

    def lock(self):
        """ Lock the wallet
            Set all parameters to default values
        """
        self.words = None
        self.keys = {}
        self.passphrase = b''
        self.language = ''
        self.unspent_txs = {}
        self.spent_txs = []
        self.balance = 0
        self.last_shared_index = 0
        self.last_generated_index = 0

    def unlock(self, tx_storage, words=None, passphrase=b'', language='english'):
        """ Unlock the wallet
            Set all parameters to initialize the wallet and load the txs

            :param tx_storage: storage from where I should load the txs
            :type tx_storage: :py:class:`hathor.transaction.storage.transaction_storage.TransactionStorage`

            :param words: words to generate the seed. It's a string with the words separated by a single space.
            If None we generate new words when starting the wallet
            :type words: string

            :param language: language of the words
            :type language: string

            :param passphrase: one more security level to generate the seed
            :type passphrase: bytes

            :return: hd wallet words. Generated in this method or passed as parameter
            :rtype: string
        """
        self.language = language
        if not words:
            # Decide to choose words automatically
            # Can be a different language than self.mnemonic
            m = Mnemonic(self.language)
            # We can't pass the word_count to generate method, only the strength
            # Multiplying by 10.67 gives the result we expect
            words = m.generate(strength=int(self.word_count*10.67))
        self.words = words
        self.passphrase = passphrase
        self._manually_initialize()
        self.load_txs(tx_storage)
        return words

    def load_txs(self, tx_storage):
        """ Load all saved txs to fill the wallet txs

            :param tx_storage: storage from where I should load the txs
            :type tx_storage: :py:class:`hathor.transaction.storage.transaction_storage.TransactionStorage`
        """
        for tx in tx_storage._topological_sort():
            self.on_new_tx(tx)

    def validate_words(self):
        """ Validate if set of words is valid
            If words is None or is not valid we raise error

            :raises InvalidWords: when the words are invalid
        """
        if not self.words or not self.mnemonic.check(self.words):
            raise InvalidWords
