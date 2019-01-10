from collections import defaultdict
from typing import Any, Dict, Optional, Set

from hathor import protos


class TransactionMetadata:
    hash: Optional[bytes]
    spent_outputs: Dict[int, Set[bytes]]
    conflict_with: Set[bytes]
    voided_by: Set[bytes]
    received_by: Set[int]
    children: Set[bytes]
    twins: Set[bytes]
    accumulated_weight: float
    score: float
    first_block: Optional[bytes]

    def __init__(self, spent_outputs: Optional[Dict[int, Set[bytes]]] = None, hash: Optional[bytes] = None,
                 accumulated_weight: float = 0, score: float = 0) -> None:

        # Hash of the transaction.
        self.hash = hash

        # Tx outputs that have been spent.
        # The key is the output index, while the value is a set of the transactions which spend the output.
        self.spent_outputs = spent_outputs or defaultdict(set)

        # FIXME: conflict_with -> conflicts_with (as in "this transaction conflicts with these ones")
        # Hash of the transactions that conflicts with this transaction.
        self.conflict_with = set()

        # Hash of the transactions that void this transaction.
        #
        # When a transaction has a conflict and is voided because of this conflict, its own hash is added to
        # voided_by. The logic is that the transaction is voiding itself.
        #
        # When a block is voided, its own hash is added to voided_by.
        self.voided_by = set()

        # List of peers which have sent this transaction.
        # Store only the peers' id.
        self.received_by = set()

        # List of transactions which have this transaction as parent.
        # Store only the transactions' hash.
        self.children = set()

        # Hash of the transactions that are twin to this transaction.
        # Twin transactions have the same inputs and outputs
        self.twins = set()

        # Accumulated weight
        self.accumulated_weight = accumulated_weight

        # Score
        self.score = score

        # First valid block that verifies this transaction
        # If two blocks verify the same parent block and have the same score, both are valid.
        self.first_block = None

    def __eq__(self, other):
        """Override the default Equals behavior"""
        for field in ['hash', 'spent_outputs', 'conflict_with', 'voided_by',
                      'received_by', 'children', 'accumulated_weight', 'twins',
                      'score', 'first_block']:
            if getattr(self, field) != getattr(other, field):
                return False
        return True

    def to_json(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        data['hash'] = self.hash and self.hash.hex()
        data['spent_outputs'] = []
        for idx, hashes in self.spent_outputs.items():
            data['spent_outputs'].append([idx, [h_bytes.hex() for h_bytes in hashes]])
        data['received_by'] = list(self.received_by)
        data['children'] = [x.hex() for x in self.children]
        data['conflict_with'] = [x.hex() for x in self.conflict_with]
        data['voided_by'] = [x.hex() for x in self.voided_by]
        data['twins'] = [x.hex() for x in self.twins]
        data['accumulated_weight'] = self.accumulated_weight
        data['score'] = self.score
        if self.first_block is not None:
            data['first_block'] = self.first_block.hex()
        else:
            data['first_block'] = None
        return data

    @classmethod
    def create_from_json(cls, data: Dict[str, Any]) -> 'TransactionMetadata':
        meta = cls()
        meta.hash = bytes.fromhex(data['hash'])
        for idx, hashes in data['spent_outputs']:
            for h_hex in hashes:
                meta.spent_outputs[idx].add(bytes.fromhex(h_hex))
        meta.received_by = set(data['received_by'])
        meta.children = set(bytes.fromhex(h) for h in data['children'])

        if 'conflict_with' in data:
            meta.conflict_with = set(bytes.fromhex(h) for h in data['conflict_with'])
        else:
            meta.conflict_with = set()

        if 'voided_by' in data:
            meta.voided_by = set(bytes.fromhex(h) for h in data['voided_by'])
        else:
            meta.voided_by = set()

        if 'twins' in data:
            meta.twins = set(bytes.fromhex(h) for h in data['twins'])
        else:
            meta.twins = set()

        meta.accumulated_weight = data['accumulated_weight']
        meta.score = data.get('score', 0)

        first_block_raw = data.get('first_block', None)
        if first_block_raw:
            meta.first_block = bytes.fromhex(first_block_raw)

        return meta

    # XXX(jansegre): I did not put the transaction hash in the protobuf object to keep it less redundant. Is this OK?
    @classmethod
    def create_from_proto(cls, hash_bytes: bytes, metadata_proto: protos.Metadata) -> 'TransactionMetadata':
        """ Create a TransactionMetadata from a protobuf Metadata object.

        :param hash_bytes: hash of the transaction in bytes
        :type hash_bytes: bytes

        :param metadata_proto: Protobuf transaction object
        :type metadata_proto: :py:class:`hathor.protos.Metadata`

        :return: A transaction metadata
        :rtype: TransactionMetadata
        """
        metadata = cls(hash=hash_bytes)
        for i, hashes in metadata_proto.spent_outputs.items():
            metadata.spent_outputs[i] = set(hashes.hashes)
        metadata.conflict_with = set(metadata_proto.conflicts_with.hashes)
        metadata.voided_by = set(metadata_proto.voided_by.hashes)
        metadata.twins = set(metadata_proto.twins.hashes)
        metadata.received_by = set(metadata_proto.received_by)
        metadata.children = set(metadata_proto.children.hashes)
        metadata.accumulated_weight = metadata_proto.accumulated_weight
        metadata.score = metadata_proto.score
        metadata.first_block = metadata_proto.first_block or None
        return metadata

    def to_proto(self) -> protos.Metadata:
        """ Creates a Probuf object from self

        :return: Protobuf object
        :rtype: :py:class:`hathor.protos.Metadata`
        """
        from hathor import protos
        return protos.Metadata(
            spent_outputs={k: protos.Metadata.Hashes(hashes=v)
                           for k, v in self.spent_outputs.items()},
            conflicts_with=protos.Metadata.Hashes(hashes=self.conflict_with),
            voided_by=protos.Metadata.Hashes(hashes=self.voided_by),
            twins=protos.Metadata.Hashes(hashes=self.twins),
            received_by=self.received_by,
            children=protos.Metadata.Hashes(hashes=self.children),
            accumulated_weight=self.accumulated_weight,
            score=self.score,
            first_block=self.first_block,
        )

    def clone(self) -> 'TransactionMetadata':
        """Return exact copy without sharing memory.

        :return: TransactionMetadata
        :rtype: :py:class:`hathor.transaction.TransactionMetadata`
        """
        # XXX: using json serialization for simplicity, should it use pickle? manual fields? other alternative?
        return self.create_from_json(self.to_json())
