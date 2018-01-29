from evm.exceptions import (
    ValidationError,
)


def validate_frontier_transaction(vm_state, transaction):
    validate_frontier_message(vm_state, transaction)
    with vm_state.state_db(read_only=True) as state_db:
        if state_db.get_nonce(transaction.sender) != transaction.nonce:
            raise ValidationError("Invalid transaction nonce")


def validate_frontier_message(vm_state, message):
    """
    Validating a message is simpler than validating a transaction as a message has no nonce.
    """
    gas_cost = message.gas * message.gas_price
    with vm_state.state_db(read_only=True) as state_db:
        sender_balance = state_db.get_balance(message.sender)

    if sender_balance < gas_cost:
        raise ValidationError(
            "Sender account balance cannot afford txn gas: `{0}`".format(message.sender)
        )

    total_cost = message.value + gas_cost

    if sender_balance < total_cost:
        raise ValidationError("Sender account balance cannot afford txn")

    if vm_state.gas_used + message.gas > vm_state.gas_limit:
        raise ValidationError("Message exceeds gas limit")

