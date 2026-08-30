import os
import shutil
import time


def cleanup_old_files(upload_dir, output_dir, temp_dir, max_age_hours):
    threshold = time.time() - max_age_hours * 3600

    for label, directory in (("uploads", upload_dir), ("output", output_dir), ("temp", temp_dir)):
        if not os.path.isdir(directory):
            continue

        count = 0
        freed = 0

        try:
            entries = os.listdir(directory)
        except OSError:
            continue

        for name in entries:
            path = os.path.join(directory, name)
            try:
                if os.path.getmtime(path) >= threshold:
                    continue
                if os.path.isfile(path) or os.path.islink(path):
                    freed += os.path.getsize(path)
                    os.remove(path)
                elif os.path.isdir(path):
                    freed += _dir_size(path)
                    shutil.rmtree(path, ignore_errors=True)
                count += 1
            except OSError:
                continue

        print(f"[cleanup] {label}: removed {count} object(s), freed {freed / 1024 / 1024:.1f} MB")


def _dir_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total
