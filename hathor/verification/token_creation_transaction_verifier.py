#  Copyright 2023 Hathor Labs
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from hathor.transaction.token_creation_tx import TokenCreationTransaction
from hathor.verification.transaction_verifier import TransactionVerifier


class TokenCreationTransactionVerifier(TransactionVerifier):
    __slots__ = ()

    def verify(self, tx: TokenCreationTransaction, *, reject_locked_reward: bool = True) -> None:
        """ Run all validations as regular transactions plus validation on token info.

        We also overload verify_sum to make some different checks
        """
        super().verify(tx, reject_locked_reward=reject_locked_reward)
        tx.verify_token_info()