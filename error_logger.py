import traceback
import sys

def log_error(context_msg: str, exc: Exception = None):
    """Log an error to errors.log and print a short version to stdout."""
    with open("errors.log", "a", encoding="utf-8") as f:
        f.write(f"{context_msg}\n")
        if exc:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
        f.write("\n" + "="*50 + "\n")
    
    # Also print the short message so it still shows in the console/main logs
    print(f"⚠ ERROR: {context_msg} (See errors.log for details)", file=sys.stderr)
