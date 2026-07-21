import re

with open("src/aml/cloud/orchestrator.py", "r") as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    if "conn = self._get_conn()" in line:
        indent = line[:len(line) - len(line.lstrip())]
        
        # Replace the line with the context manager
        new_lines.append(indent + "with self._get_conn() as conn:\n")
        
        # Now indent all following lines until we hit a line that is indented the same or less, 
        # or we hit a return statement (wait, if there's a return statement, we indent it and then stop!)
        i += 1
        while i < len(lines):
            next_line = lines[i]
            if not next_line.strip():
                new_lines.append(next_line)
                i += 1
                continue
            
            next_indent_len = len(next_line) - len(next_line.lstrip())
            if next_indent_len <= len(indent):
                # End of block
                break
                
            # It's indented more or equal (wait, if it's equal it might be the next statement)
            # Actually, we want to indent all statements that are part of the current logical block.
            # A simple heuristic: we just indent the IMMEDIATELY following statements that belong to this function's scope.
            # But it's safer to just indent everything until the end of the function or until we see another `conn = self._get_conn()`.
            pass
        # Actually this is too risky to do with a simple script. I will do it with multi_replace_file_content!
