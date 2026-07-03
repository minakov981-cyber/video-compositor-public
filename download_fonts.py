"""Download a curated set of Google Fonts into the fonts/ directory."""
import os
import urllib.request

FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
BASE = "https://raw.githubusercontent.com/google/fonts/main"

FONTS = [
    ("Anton-Regular.ttf",           f"{BASE}/ofl/anton/Anton-Regular.ttf"),
    ("BebasNeue-Regular.ttf",        f"{BASE}/ofl/bebasneue/BebasNeue-Regular.ttf"),
    ("Poppins-Bold.ttf",             f"{BASE}/ofl/poppins/Poppins-Bold.ttf"),
    ("Poppins-SemiBoldItalic.ttf",   f"{BASE}/ofl/poppins/Poppins-SemiBoldItalic.ttf"),
    ("Lato-Black.ttf",               f"{BASE}/ofl/lato/Lato-Black.ttf"),
    ("TitilliumWeb-Black.ttf",       f"{BASE}/ofl/titilliumweb/TitilliumWeb-Black.ttf"),
    ("Barlow-Black.ttf",             f"{BASE}/ofl/barlow/Barlow-Black.ttf"),
    ("RussoOne-Regular.ttf",         f"{BASE}/ofl/russoone/RussoOne-Regular.ttf"),
    ("Arvo-Bold.ttf",                f"{BASE}/ofl/arvo/Arvo-Bold.ttf"),
    ("Kanit-Bold.ttf",               f"{BASE}/ofl/kanit/Kanit-Bold.ttf"),
]

os.makedirs(FONTS_DIR, exist_ok=True)

for filename, url in FONTS:
    dest = os.path.join(FONTS_DIR, filename)
    if os.path.isfile(dest):
        print(f"  skip  {filename}")
        continue
    print(f"  fetch {filename} … ", end="", flush=True)
    try:
        urllib.request.urlretrieve(url, dest)
        print("ok")
    except Exception as e:
        print(f"FAILED ({e})")

print("Done.")
