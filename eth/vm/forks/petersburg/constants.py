from eth_utils import denoms
from eth_typing import (
    Address
)


GAS_EXTCODEHASH_EIP1052 = 400

EIP1234_BLOCK_REWARD = 2 * denoms.ether

# Currently no reward is issued.
EIP1789_DEVFUND_REWARD = 0
EIP1789_DEVFUND_BENEFICIARY = Address(b'')
