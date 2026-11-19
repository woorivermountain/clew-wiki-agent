"""Domain holds that require a source/approval review rather than a blind retry."""


class RevalidationRequiredError(ValueError):
    def __init__(self, message: str, *, code: str = "revalidation_required"):
        super().__init__(message)
        self.code = code
