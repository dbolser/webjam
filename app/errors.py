"""Machine-readable API errors.

`detail` stays a string (the SPA's api() helper prints it); `code` and any extra
fields sit next to it at the top level of the JSON body.
"""

from typing import Any, Dict, Optional

from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        headers: Optional[Dict[str, str]] = None,
        **extra: Any,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.extra = extra

    def body(self) -> Dict[str, Any]:
        return {"detail": self.detail, "code": self.code, **self.extra}
