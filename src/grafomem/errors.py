class LetheError(Exception):
    """Base class for all Grafomem runtime errors."""
    pass

class SignatureMismatch(LetheError, ValueError):
    """Raised when a CSO signature fails verification."""
    pass

class UnknownKey(LetheError, ValueError):
    """Raised when a CSO claims a key_id that is not in the trusted store."""
    pass

class InfeasibleSchedule(LetheError, RuntimeError):
    """Raised when the scheduler cannot find a valid cover within the budget V."""
    pass

class PolicyViolation(LetheError, RuntimeError):
    """Raised when a link violates the governance policy."""
    pass
