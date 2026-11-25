"""Domain holds that require a source/approval review rather than a blind retry."""


class RevalidationRequiredError(ValueError):
    def __init__(self, message: str, *, code: str = "revalidation_required"):
        super().__init__(message)
        self.code = code



class RequiredEvidenceMissingError(RuntimeError):
    """The candidate wiki omitted required evidence for one or more assets."""

    def __init__(self, findings: list[dict]) -> None:
        super().__init__(f"필수 근거 묶음 {len(findings)}건이 빠져 위키 반영을 보류했습니다.")
        self.findings = findings
