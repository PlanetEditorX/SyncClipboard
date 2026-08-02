import os
def secure_save_path(base_dir, user_path):
    if not user_path:
        return None
    user_path = user_path.replace('\\', '/')
    base_dir = os.path.abspath(base_dir)
    # wait, if user_path starts with a slash, os.path.join ignores base_dir!
    # Let's test os.path.join('/tmp/save', '/etc/passwd')
    target_path = os.path.abspath(os.path.join(base_dir, user_path.lstrip('/')))
    if not target_path.startswith(base_dir + os.sep):
        if target_path != base_dir:
            return None
    return target_path

print(secure_save_path("/tmp/save", "../../etc/passwd"))
print(secure_save_path("/tmp/save", "/etc/passwd"))
print(os.path.join("/tmp/save", "/etc/passwd"))
