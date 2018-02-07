from __future__ import absolute_import

import contextlib
import logging
import rlp

from eth_utils import (
    to_tuple,
)

from evm.constants import (
    CREATE_CONTRACT_ADDRESS,
    GENESIS_PARENT_HASH,
    MAX_PREV_HEADER_DEPTH,
    MAX_UNCLES,
    ZERO_ADDRESS,
)
from evm.exceptions import (
    BlockNotFound,
    ValidationError,
)
from evm.db.backends.memory import MemoryDB
from evm.db.chain import ChainDB
from evm.rlp.headers import (
    BlockHeader,
)
from evm.utils.address import (
    generate_contract_address,
)
from evm.utils.datatypes import (
    Configurable,
)
from evm.utils.db import (
    get_parent_header,
    get_block_header_by_hash,
)
from evm.utils.headers import (
    generate_header_from_parent_header,
)
from evm.utils.keccak import (
    keccak,
)
from evm.validation import (
    validate_canonical_address,
    validate_gas_limit,
    validate_is_bytes,
    validate_length_lte,
    validate_uint256,
)
from evm.vm.message import (
    Message,
)

from .execution_context import (
    ExecutionContext,
)


class VM(Configurable):
    """
    The VM class represents the Chain rules for a specific protocol definition
    such as the Frontier or Homestead network.  Define a Chain defining
    individual VM classes for each fork of the protocol rules within that
    network.
    """
    chaindb = None
    _block_class = None
    _state_class = None

    def __init__(self, header, chaindb):
        self.chaindb = chaindb
        block_class = self.get_block_class()
        self.block = block_class.from_header(header=header, chaindb=self.chaindb)

    #
    # Logging
    #
    @property
    def logger(self):
        return logging.getLogger('evm.vm.base.VM.{0}'.format(self.__class__.__name__))

    #
    # Execution
    #
    def apply_transaction(self, transaction):
        """
        Apply the transaction to the vm in the current block.
        """
        computation, block, trie_data_dict = self.get_state_class().apply_transaction(
            self.state,
            transaction,
            self.block,
        )
        self.block = block

        # Persist changed transaction and receipt key-values to self.chaindb.
        self.chaindb.persist_trie_data_dict_to_db(trie_data_dict)

        self.clear_journal()

        return computation, self.block

    def execute_bytecode(self,
                         bytecode,
                         gas,
                         gas_price,
                         to,
                         sender,
                         value,
                         data,
                         origin,
                         # create_address,
                         code_address=None,
                         ):
        """
        Run EVM bytecode.

        :param int gas: the amount of gas left
        :param int gas_price: the price per unit gas
        :param bytes to:
        :param bytes sender:
        :param int value:
        :param bytes data:
        :param bytes code:
        :param bytes origin: 20-byte public address
        :param int depth:
        :param bytes create_address:
        :param bytes code_address:
        """
        if gas is None:
            gas = self.block.header.gas_limit
        if gas_price is None:
            gas_price = 1
        if to is None:
            to = ZERO_ADDRESS
        if sender is None:
            sender = ZERO_ADDRESS
        if value is None:
            value = 0
        if data is None:
            data = b''
        if origin is None:
            origin = ZERO_ADDRESS

        # Validate the inputs
        validate_uint256(self.gas_price, title="Transaction.gas_price")
        validate_uint256(self.gas, title="Transaction.gas")
        if self.to != CREATE_CONTRACT_ADDRESS:
            validate_canonical_address(self.to, title="Transaction.to")
        validate_uint256(self.value, title="Transaction.value")
        validate_is_bytes(self.data, title="Transaction.data")
        # if _get_frontier_intrinsic_gas() > gas:
        #     raise ValidationError("Insufficient gas")

        # TODO: Unclear whether this step is necessary! Not sure yet how much validation we want to
        # do.
        # self.state.validate_transaction()

        # Pre computation
        gas_fee = gas * gas_price
        with self.state.state_db() as state_db:
            # Buy Gas
            state_db.delta_balance(sender, -1 * gas_fee)

            # Increment Nonce
            state_db.increment_nonce(sender)

            if to == CREATE_CONTRACT_ADDRESS:
                contract_address = generate_contract_address(
                    sender,
                    state_db.get_nonce(sender) - 1,
                )
                data = b''
                code = data
            else:
                contract_address = code_address
                code = bytecode

        # Construct a message
        message = Message(
            gas=gas,
            gas_price=gas_price,
            to=to,
            sender=sender,
            value=value,
            data=data,
            code=code,
            create_address=contract_address,
            code_address=code_address,
        )

        # Execute it in the VM
        if message.is_create:
            computation = self.state.get_computation(message).apply_create_message()
        else:
            computation = self.state.get_computation(message).apply_message()

        # Return the result
        return computation

    #
    # Mining
    #
    def import_block(self, block):
        self.configure_header(
            coinbase=block.header.coinbase,
            gas_limit=block.header.gas_limit,
            timestamp=block.header.timestamp,
            extra_data=block.header.extra_data,
            mix_hash=block.header.mix_hash,
            nonce=block.header.nonce,
            uncles_hash=keccak(rlp.encode(block.uncles)),
        )

        # run all of the transactions.
        for transaction in block.transactions:
            self.apply_transaction(transaction)

        # transfer the list of uncles.
        self.block.uncles = block.uncles

        return self.mine_block()

    def mine_block(self, *args, **kwargs):
        """
        Mine the current block. Proxies to self.pack_block method.
        """
        block = self.block
        self.pack_block(block, *args, **kwargs)

        if block.number == 0:
            return block

        block = self.state.finalize_block(block)

        return block

    def pack_block(self, block, *args, **kwargs):
        """
        Pack block for mining.

        :param bytes coinbase: 20-byte public address to receive block reward
        :param bytes uncles_hash: 32 bytes
        :param bytes state_root: 32 bytes
        :param bytes transaction_root: 32 bytes
        :param bytes receipt_root: 32 bytes
        :param int bloom:
        :param int gas_used:
        :param bytes extra_data: 32 bytes
        :param bytes mix_hash: 32 bytes
        :param bytes nonce: 8 bytes
        """
        if 'uncles' in kwargs:
            block.uncles = kwargs.pop('uncles')
            kwargs.setdefault('uncles_hash', keccak(rlp.encode(block.uncles)))

        header = block.header
        provided_fields = set(kwargs.keys())
        known_fields = set(tuple(zip(*BlockHeader.fields))[0])
        unknown_fields = provided_fields.difference(known_fields)

        if unknown_fields:
            raise AttributeError(
                "Unable to set the field(s) {0} on the `BlockHeader` class. "
                "Received the following unexpected fields: {1}.".format(
                    ", ".join(known_fields),
                    ", ".join(unknown_fields),
                )
            )

        for key, value in kwargs.items():
            setattr(header, key, value)

        # Perform validation
        self.validate_block(block)

        return block

    @contextlib.contextmanager
    def state_in_temp_block(self):
        header = self.block.header
        temp_block = self.generate_block_from_parent_header_and_coinbase(header, header.coinbase)
        prev_hashes = (header.hash, ) + self.previous_hashes
        state = self.get_state(block_header=temp_block.header, prev_hashes=prev_hashes)
        snapshot = state.snapshot()
        yield state
        state.revert(snapshot)

    @classmethod
    def create_block(
            cls,
            transaction_packages,
            prev_hashes,
            coinbase,
            parent_header):
        """
        Create a block with transaction witness
        """
        block = cls.generate_block_from_parent_header_and_coinbase(
            parent_header,
            coinbase,
        )

        recent_trie_nodes = {}
        receipts = []
        for (transaction, transaction_witness) in transaction_packages:
            transaction_witness.update(recent_trie_nodes)
            witness_db = ChainDB(MemoryDB(transaction_witness))

            execution_context = ExecutionContext.from_block_header(block.header, prev_hashes)
            vm_state = cls.get_state_class()(
                chaindb=witness_db,
                execution_context=execution_context,
                state_root=block.header.state_root,
                receipts=receipts,
            )
            computation, result_block, _ = vm_state.apply_transaction(
                transaction=transaction,
                block=block,
            )

            if not computation.is_error:
                block = result_block
                receipts = computation.vm_state.receipts
                recent_trie_nodes.update(computation.vm_state.access_logs.writes)
            else:
                pass

        # Finalize
        witness_db = ChainDB(MemoryDB(recent_trie_nodes))
        execution_context = ExecutionContext.from_block_header(block.header, prev_hashes)
        vm_state = cls.get_state_class()(
            chaindb=witness_db,
            execution_context=execution_context,
            state_root=block.header.state_root,
            receipts=receipts,
        )
        block = vm_state.finalize_block(block)

        return block

    @classmethod
    def generate_block_from_parent_header_and_coinbase(cls, parent_header, coinbase):
        """
        Generate block from parent header and coinbase.
        """
        block_header = generate_header_from_parent_header(
            cls.compute_difficulty,
            parent_header,
            coinbase,
            timestamp=parent_header.timestamp + 1,
        )
        block = cls.get_block_class()(
            block_header,
            transactions=[],
            uncles=[],
        )
        return block

    #
    # Validate
    #
    def validate_block(self, block):
        if not block.is_genesis:
            parent_header = get_parent_header(block.header, self.chaindb)

            validate_gas_limit(block.header.gas_limit, parent_header.gas_limit)
            validate_length_lte(block.header.extra_data, 32, title="BlockHeader.extra_data")

            # timestamp
            if block.header.timestamp < parent_header.timestamp:
                raise ValidationError(
                    "`timestamp` is before the parent block's timestamp.\n"
                    "- block  : {0}\n"
                    "- parent : {1}. ".format(
                        block.header.timestamp,
                        parent_header.timestamp,
                    )
                )
            elif block.header.timestamp == parent_header.timestamp:
                raise ValidationError(
                    "`timestamp` is equal to the parent block's timestamp\n"
                    "- block : {0}\n"
                    "- parent: {1}. ".format(
                        block.header.timestamp,
                        parent_header.timestamp,
                    )
                )

        if len(block.uncles) > MAX_UNCLES:
            raise ValidationError(
                "Blocks may have a maximum of {0} uncles.  Found "
                "{1}.".format(MAX_UNCLES, len(block.uncles))
            )

        for uncle in block.uncles:
            self.validate_uncle(block, uncle)

        if not self.state.is_key_exists(block.header.state_root):
            raise ValidationError(
                "`state_root` was not found in the db.\n"
                "- state_root: {0}".format(
                    block.header.state_root,
                )
            )
        local_uncle_hash = keccak(rlp.encode(block.uncles))
        if local_uncle_hash != block.header.uncles_hash:
            raise ValidationError(
                "`uncles_hash` and block `uncles` do not match.\n"
                " - num_uncles       : {0}\n"
                " - block uncle_hash : {1}\n"
                " - header uncle_hash: {2}".format(
                    len(block.uncles),
                    local_uncle_hash,
                    block.header.uncle_hash,
                )
            )

    def validate_uncle(self, block, uncle):
        if uncle.block_number >= block.number:
            raise ValidationError(
                "Uncle number ({0}) is higher than block number ({1})".format(
                    uncle.block_number, block.number))
        try:
            parent_header = get_block_header_by_hash(uncle.parent_hash, self.chaindb)
        except BlockNotFound:
            raise ValidationError(
                "Uncle ancestor not found: {0}".format(uncle.parent_hash))
        if uncle.block_number != parent_header.block_number + 1:
            raise ValidationError(
                "Uncle number ({0}) is not one above ancestor's number ({1})".format(
                    uncle.block_number, parent_header.block_number))
        if uncle.timestamp < parent_header.timestamp:
            raise ValidationError(
                "Uncle timestamp ({0}) is before ancestor's timestamp ({1})".format(
                    uncle.timestamp, parent_header.timestamp))
        if uncle.gas_used > uncle.gas_limit:
            raise ValidationError(
                "Uncle's gas usage ({0}) is above the limit ({1})".format(
                    uncle.gas_used, uncle.gas_limit))

    #
    # Transactions
    #

    @classmethod
    def get_transaction_class(cls):
        """
        Return the class that this VM uses for transactions.
        """
        return cls.get_block_class().get_transaction_class()

    def get_pending_transaction(self, transaction_hash):
        return self.chaindb.get_pending_transaction(transaction_hash, self.get_transaction_class())

    def create_transaction(self, *args, **kwargs):
        """
        Proxy for instantiating a transaction for this VM.
        """
        return self.get_transaction_class()(*args, **kwargs)

    def create_unsigned_transaction(self, *args, **kwargs):
        """
        Proxy for instantiating a transaction for this VM.
        """
        return self.get_transaction_class().create_unsigned_transaction(*args, **kwargs)

    #
    # Blocks
    #
    @classmethod
    def get_block_class(cls):
        """
        Return the class that this VM uses for blocks.
        """
        if cls._block_class is None:
            raise AttributeError("No `_block_class` has been set for this VM")

        return cls._block_class

    @classmethod
    def get_block_by_header(cls, block_header, db):
        return cls.get_block_class().from_header(block_header, db)

    @classmethod
    @to_tuple
    def get_prev_hashes(cls, last_block_hash, db):
        if last_block_hash == GENESIS_PARENT_HASH:
            return

        block_header = get_block_header_by_hash(last_block_hash, db)

        for _ in range(MAX_PREV_HEADER_DEPTH):
            yield block_header.hash
            try:
                block_header = get_parent_header(block_header, db)
            except (IndexError, BlockNotFound):
                break

    @property
    def previous_hashes(self):
        return self.get_prev_hashes(self.block.header.parent_hash, self.chaindb)

    #
    # Gas Usage API
    #
    def get_cumulative_gas_used(self, block):
        """
        Note return value of this function can be cached based on
        `self.receipt_db.root_hash`
        """
        if len(block.transactions):
            return block.get_receipts(self.chaindb)[-1].gas_used
        else:
            return 0

    #
    # Headers
    #
    @classmethod
    def create_header_from_parent(cls, parent_header, **header_params):
        """
        Creates and initializes a new block header from the provided
        `parent_header`.
        """
        raise NotImplementedError("Must be implemented by subclasses")

    def configure_header(self, **header_params):
        """
        Setup the current header with the provided parameters.  This can be
        used to set fields like the gas limit or timestamp to value different
        than their computed defaults.
        """
        raise NotImplementedError("Must be implemented by subclasses")

    @classmethod
    def compute_difficulty(cls, parent_header, timestamp):
        raise NotImplementedError("Must be implemented by subclasses")

    #
    # Snapshot and Revert
    #
    def clear_journal(self):
        """
        Cleare the journal.  This should be called at any point of VM execution
        where the statedb is being committed, such as after a transaction has
        been applied to a block.
        """
        self.chaindb.clear()

    #
    # State
    #
    @classmethod
    def get_state_class(cls):
        """
        Return the class that this VM uses for states.
        """
        if cls._state_class is None:
            raise AttributeError("No `_state_class` has been set for this VM")

        return cls._state_class

    def get_state(self, chaindb=None, block_header=None, prev_hashes=None):
        """Return state object
        """
        if chaindb is None:
            chaindb = self.chaindb
        if block_header is None:
            block_header = self.block.header
        if prev_hashes is None:
            prev_hashes = self.get_prev_hashes(
                last_block_hash=block_header.parent_hash,
                db=chaindb,
            )

        execution_context = ExecutionContext.from_block_header(block_header, prev_hashes)
        receipts = self.block.get_receipts(self.chaindb)
        return self.get_state_class()(
            chaindb,
            execution_context=execution_context,
            state_root=block_header.state_root,
            receipts=receipts,
        )

    @property
    def state(self):
        """Return current state property
        """
        return self.get_state(
            chaindb=self.chaindb,
            block_header=self.block.header,
        )
