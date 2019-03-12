from eth_typing import (
    Address
)

from typing import (  # noqa: F401
    Type,
)

from eth.rlp.blocks import BaseBlock  # noqa: F401
from eth.vm.forks.byzantium import (
    ByzantiumVM,
    get_uncle_reward,
)
from eth.vm.state import BaseState  # noqa: F401

from .blocks import PetersburgBlock
from .constants import EIP1234_BLOCK_REWARD, EIP1789_DEVFUND_REWARD, EIP1789_DEVFUND_BENEFICIARY
from .headers import (
    compute_petersburg_difficulty,
    configure_petersburg_header,
    create_petersburg_header_from_parent,
)
from .state import PetersburgState


class PetersburgVM(ByzantiumVM):
    # fork name
    fork = 'petersburg'

    # classes
    block_class = PetersburgBlock  # type: Type[BaseBlock]
    _state_class = PetersburgState  # type: Type[BaseState]

    # Methods
    create_header_from_parent = staticmethod(create_petersburg_header_from_parent)  # type: ignore  # noqa: E501
    compute_difficulty = staticmethod(compute_petersburg_difficulty)    # type: ignore
    configure_header = configure_petersburg_header
    get_uncle_reward = staticmethod(get_uncle_reward(EIP1234_BLOCK_REWARD))

    @staticmethod
    def get_block_reward() -> int:
        return EIP1234_BLOCK_REWARD

    @staticmethod
    def get_devfund_reward() -> int:
        return EIP1789_DEVFUND_REWARD

    @staticmethod
    def get_devfund_beneficiary() -> Address:
        return EIP1789_DEVFUND_BENEFICIARY

    #
    # Finalization
    #
    def finalize_block(self, block: BaseBlock) -> BaseBlock:
        """
        Perform any finalization steps like awarding the block mining reward.
        """
        devfund_reward = self.get_devfund_reward()
        devfund_beneficiary = self.get_devfund_beneficiary()

        self.state.account_db.delta_balance(devfund_beneficiary, devfund_reward)
        self.logger.debug(
            "DEVDFUND REWARD: %s -> %s",
            devfund_reward,
            devfund_beneficiary,
        )

        return super(block)
