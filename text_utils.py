import re

def clean_metadata_text(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r'\s*[\(-]\s*(Translated|Adapted|Original)(\s*version)?\s*\)?(\s*version)?', '', text, flags=re.IGNORECASE)
    return clean.strip()

def clean_and_resolve_title(text: str, key: str = "") -> str:
    if not text:
        clean = ""
    else:
        clean = clean_metadata_text(text)
    
    is_generic = False
    if not clean:
        is_generic = True
    elif clean.lower() == "general":
        is_generic = True
    elif key and "_general" in key.lower(): 
         pass 
         
    if is_generic and key:
        parts = re.split(r'[-:]', key)
        candidate = parts[-1]
        
        if candidate.isdigit() and len(parts) > 1:
            candidate = parts[-2]
            
        clean = candidate.replace('_', ' ').strip().title()
        
    return clean

def format_mobile_markdown(title: str, content: str) -> str:
    content = (content or "").strip()
    
    def normalize_chapter_line(line_content):
        if line_content.lower().startswith("# chapter:"):
             return re.sub(r"^#\s*chapter:", "# Chapter:", line_content, count=1, flags=re.IGNORECASE)
        return line_content

    h1_match = re.match(r'^#\s*(.+?)(\n|$)', content)
    chapter_match = re.match(r'^#\s*Chapter:', content, re.IGNORECASE)
    
    if chapter_match:
        first_line_end = content.find('\n')
        if first_line_end == -1:
            first_line = content
            rest = ""
        else:
            first_line = content[:first_line_end]
            rest = content[first_line_end:]
            
        first_line = normalize_chapter_line(first_line)
        return f"# {title}\n{first_line}{rest}"

    if h1_match:
        remainder = content[h1_match.end():].strip()
        
        if re.match(r'^#\s*Chapter:', remainder, re.IGNORECASE):
            first_n_end = remainder.find('\n')
            if first_n_end == -1:
                first_line = remainder
                rest = ""
            else:
                first_line = remainder[:first_n_end]
                rest = remainder[first_n_end:]
                
            first_line = normalize_chapter_line(first_line)
            return f"# {title}\n{first_line}{rest}"
        else:
            chapter_title = h1_match.group(1).strip()
            return f"# {title}\n# Chapter: {chapter_title}\n\n{remainder}"
        
    return f"# {title}\n# Chapter: {title}\n\n{content}"
