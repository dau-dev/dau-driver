from __future__ import annotations


class DauDriverError(Exception):
    pass


class DeviceNotFoundError(DauDriverError):
    pass


class RegisterAccessError(DauDriverError):
    pass


class TransferValidationError(DauDriverError):
    pass


class TransferIoError(DauDriverError):
    pass


class WrongBitstreamError(DauDriverError):
    pass
