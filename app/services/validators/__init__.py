"""P1D/P2D/P3C.2：Validator 抽象、引用校验与回答内容校验。"""

from app.services.validators.base import Issue, ValidationReport, Validator
from app.services.validators.citation import CitationValidator
from app.services.validators.content import (
    CONTENT_VERSION,
    ContentValidator,
    failure_kind,
    failure_message,
    validate_content,
    warning_codes,
)
from app.services.validators.labels import (
    DEFAULT_LABEL_PREFIXES,
    LABEL_VERSION,
    LabelCitationValidator,
)

__all__ = [
    "Issue",
    "ValidationReport",
    "Validator",
    "CitationValidator",
    "CONTENT_VERSION",
    "ContentValidator",
    "DEFAULT_LABEL_PREFIXES",
    "LABEL_VERSION",
    "LabelCitationValidator",
    "failure_kind",
    "failure_message",
    "validate_content",
    "warning_codes",
]
