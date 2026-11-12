"""Domain errors. The API turns them into 4xx responses with the message as `detail`."""


class SupplyError(Exception):
    status_code = 422

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(SupplyError):
    status_code = 404


class Conflict(SupplyError):
    status_code = 409


class Forbidden(SupplyError):
    status_code = 403
