import traceback
import sys

_LOG_PREFIX = ""

def set_log_prefix(prefix: str):
    """Set a global prefix (e.g., language name and stage) for all logs."""
    global _LOG_PREFIX
    _LOG_PREFIX = f"[{prefix}] " if prefix else ""

def log_error(context_msg: str, exc: Exception = None):
    """Log an error to errors.log and print a short version to stdout."""
    # Check for 404 Noise Reduction (Missing Azure Blobs)
    if exc:
        is_404 = False
        if hasattr(exc, "response") and getattr(exc.response, "status_code", None) == 404:
            is_404 = True
        elif "404" in str(exc) and "Client Error" in str(exc):
            is_404 = True
            
        if is_404:
            warning_msg = f"{_LOG_PREFIX}⚠ WARNING: 404 Missing Resource: {context_msg}"
            print(warning_msg)
            with open("errors.log", "a", encoding="utf-8") as f:
                f.write(f"{warning_msg} (Skipped stacktrace for clarity)\n")
            return

    final_msg = f"{_LOG_PREFIX}{context_msg}"
    with open("errors.log", "a", encoding="utf-8") as f:
        f.write(f"{final_msg}\n")
        if exc:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
        f.write("\n" + "="*50 + "\n")
    
    # Also print the short message so it still shows in the console/main logs
    print(f"⚠ ERROR: {final_msg} (See errors.log for details)", file=sys.stderr)
