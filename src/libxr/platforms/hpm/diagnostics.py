"""Machine-readable diagnostics for the HPM CLI protocol."""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

ERROR = "error"
WARNING = "warning"
_LEVELS = frozenset((ERROR, WARNING))


@dataclass(frozen=True)
class Diagnostic:
    """A stable diagnostic record returned by HPM CLI commands."""

    code: str
    level: str
    message: str
    peripheral: Optional[str] = None
    field: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("diagnostic code must be a non-empty string")
        if self.level not in _LEVELS:
            raise ValueError("diagnostic level must be 'error' or 'warning'")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("diagnostic message must be a non-empty string")
        if self.peripheral is not None and not isinstance(self.peripheral, str):
            raise TypeError("diagnostic peripheral must be a string or None")
        if self.field is not None and not isinstance(self.field, str):
            raise TypeError("diagnostic field must be a string or None")

    def to_dict(self) -> Dict[str, Optional[str]]:
        """Return all protocol fields, including absent optional fields."""

        return {
            "code": self.code,
            "level": self.level,
            "peripheral": self.peripheral,
            "field": self.field,
            "message": self.message,
        }


def diagnostics_to_dicts(
    diagnostics: Iterable[Diagnostic],
) -> List[Dict[str, Optional[str]]]:
    """Convert diagnostics to JSON-compatible protocol records."""

    return [diagnostic.to_dict() for diagnostic in diagnostics]
