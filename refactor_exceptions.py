import os
import glob

def refactor_exceptions():
    files = glob.glob("c:/Users/vikra/Developer/dev/content_migrator/**/*.py", recursive=True)
    
    for file in files:
        if "error_logger.py" in file or "refactor.py" in file:
            continue
            
        with open(file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        new_lines = []
        modified = False
        
        for i, line in enumerate(lines):
            new_lines.append(line)
            
            stripped = line.strip()
            if stripped.startswith("except Exception as ") or stripped == "except Exception:":
                indent = line[:len(line) - len(line.lstrip())]
                new_indent = indent + "    "
                
                # Check if we already injected
                if i + 1 < len(lines) and "from error_logger import log_error" in lines[i+1]:
                    continue
                    
                new_lines.append(new_indent + "from error_logger import log_error\n")
                
                if "as " in stripped:
                    exc_var = stripped.split("as ")[1].strip().strip(":")
                    new_lines.append(new_indent + f'log_error("Captured Exception", exc={exc_var})\n')
                else:
                    new_lines.append(new_indent + 'log_error("Captured Exception")\n')
                
                modified = True
                
        if modified:
            with open(file, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            print(f"Refactored: {file}")

if __name__ == "__main__":
    refactor_exceptions()
    print("Done refactoring exceptions.")
