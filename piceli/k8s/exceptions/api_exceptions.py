import json
from dataclasses import dataclass
from enum import Enum

from kubernetes.client.exceptions import ApiException


class ReasonEnum(Enum):
    AlreadyExists = "AlreadyExists"
    NotFound = "NotFound"
    Unknown = None


@dataclass
class ApiOperationException(Exception):
    code: int
    status: str
    reason: str
    message: str
    details: dict
    ex: ApiException
    body: dict

    @classmethod
    def from_api_exception(cls, ex: ApiException) -> "ApiOperationException":
        body = json.loads(ex.body)
        return cls(
            code=body.get("code", ex.status),
            status=body.get("status", ""),
            reason=body.get("reason", ex.reason),
            message=body.get("message", ""),
            details=body.get("details", {}),
            ex=ex,
            body=body,
        )

    @property
    def not_found(self) -> bool:
        return self.reason == ReasonEnum.NotFound.value

    @property
    def already_exists(self) -> bool:
        return self.reason == ReasonEnum.AlreadyExists.value

    @property
    def is_being_deleted(self) -> bool:
        return "object is being deleted" in self.message
